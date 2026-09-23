#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Opt-in, bounded CUDA kernel/copy correctness check; no device reset or reboot."""
import argparse
import ctypes as C
import json
from pathlib import Path
import subprocess
import sys


def worker(expected_gpus):
    cuda = C.CDLL('libcuda.so.1')
    P, I, U, D = C.c_void_p, C.c_int, C.c_uint, C.c_uint64
    def bind(name, args):
        fn = getattr(cuda, name)
        fn.argtypes, fn.restype = args, I
        return fn
    error_name = bind('cuGetErrorName', [I, C.POINTER(C.c_char_p)])
    def call(fn, *args):
        result = fn(*args)
        if result:
            error = C.c_char_p()
            error_name(result, C.byref(error))
            raise RuntimeError(f'{fn.__name__}: {result} {error.value!r}')

    init = bind('cuInit', [U])
    get_count = bind('cuDeviceGetCount', [C.POINTER(I)])
    get_version = bind('cuDriverGetVersion', [C.POINTER(I)])
    get_device = bind('cuDeviceGet', [C.POINTER(I), I])
    get_bus = bind('cuDeviceGetPCIBusId', [C.c_char_p, I, I])
    create_ctx = bind('cuCtxCreate_v2', [C.POINTER(P), U, I])
    destroy_ctx = bind('cuCtxDestroy_v2', [P])
    alloc = bind('cuMemAlloc_v2', [C.POINTER(D), C.c_size_t])
    htod = bind('cuMemcpyHtoD_v2', [D, P, C.c_size_t])
    dtoh = bind('cuMemcpyDtoH_v2', [P, D, C.c_size_t])
    load = bind('cuModuleLoadData', [C.POINTER(P), P])
    function = bind('cuModuleGetFunction', [C.POINTER(P), P, C.c_char_p])
    launch = bind('cuLaunchKernel', [P, U, U, U, U, U, U, U, P, C.POINTER(P), C.POINTER(P)])
    sync = bind('cuCtxSynchronize', [])
    peer = bind('cuDeviceCanAccessPeer', [C.POINTER(I), I, I])
    ptx = C.create_string_buffer(b'''
    .version 7.0
    .target sm_80
    .address_size 64
    .visible .entry increment(.param .u64 buffer, .param .u32 count) {
     .reg .pred %p;
     .reg .b32 %r<7>;
     .reg .b64 %rd<4>;
     ld.param.u64 %rd1, [buffer];
     ld.param.u32 %r1, [count];
     mov.u32 %r2, %ctaid.x;
     mov.u32 %r3, %ntid.x;
     mov.u32 %r4, %tid.x;
     mad.lo.s32 %r5, %r2, %r3, %r4;
     setp.ge.u32 %p, %r5, %r1;
     @%p bra done;
     mul.wide.u32 %rd2, %r5, 4;
     add.u64 %rd3, %rd1, %rd2;
     ld.global.u32 %r6, [%rd3];
     add.u32 %r6, %r6, 1;
     st.global.u32 [%rd3], %r6;
    done:
     ret;
    }
    ''')
    call(init, 0)
    count, version = I(), I()
    call(get_count, C.byref(count))
    call(get_version, C.byref(version))
    print(json.dumps({'cuda_driver_api': version.value, 'gpu_count': count.value}), flush=True)
    if count.value != expected_gpus:
        raise RuntimeError(f'Expected {expected_gpus} CUDA devices, found {count.value}')
    n = 1 << 20
    for index in range(count.value):
        device, ctx, module, kernel, pointer, size = I(), P(), P(), P(), D(), U(n)
        bus = C.create_string_buffer(32)
        call(get_device, C.byref(device), index)
        call(get_bus, bus, len(bus), device)
        call(create_ctx, C.byref(ctx), 0, device)
        try:
            host = (U * n)(*(i % 1009 for i in range(n)))
            call(alloc, C.byref(pointer), C.sizeof(host))
            call(htod, pointer, C.cast(host, P), C.sizeof(host))
            call(load, C.byref(module), C.cast(ptx, P))
            call(function, C.byref(kernel), module, b'increment')
            params = (P * 2)(C.addressof(pointer), C.addressof(size))
            call(launch, kernel, (n + 255) // 256, 1, 1, 256, 1, 1, 0, None, params, None)
            call(sync)
            call(dtoh, C.cast(host, P), pointer, C.sizeof(host))
            if not all(value == (i % 1009) + 1 for i, value in enumerate(host)):
                raise RuntimeError(f'Incorrect result on GPU {index}')
            print(json.dumps({'gpu': index, 'pci': bus.value.decode(), 'elements_verified': n,
                              'bytes_tested': C.sizeof(host), 'kernel_copy_result': 'PASS'}), flush=True)
        finally:
            call(destroy_ctx, ctx)
    matrix = []
    for a in range(count.value):
        row = []
        for b in range(count.value):
            value = I()
            if a != b:
                call(peer, C.byref(value), a, b)
            row.append(value.value)
        matrix.append(row)
    print(json.dumps({'peer_access_capability_matrix': matrix, 'peer_transfer_tested': False}), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Run GPU workloads; omitted means plan only.')
    parser.add_argument('--expected-gpus', type=int, default=4)
    parser.add_argument('--timeout-seconds', type=int, default=180)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.expected_gpus <= 8 or not 1 <= args.timeout_seconds <= 1800:
        parser.error('Expected GPU count must be 1..8 and timeout must be 1..1800 seconds.')
    if not args.execute:
        print(f'Plan: verify CUDA kernel/copies on exactly {args.expected_gpus} GPUs; report peer capability; timeout {args.timeout_seconds}s.')
        return 0
    if args.worker:
        worker(args.expected_gpus)
        return 0
    try:
        apps = subprocess.check_output(['nvidia-smi', '--query-compute-apps=pid', '--format=csv,noheader'], text=True, timeout=20)
        if apps.strip():
            raise RuntimeError('GPU compute processes already exist; use an idle validation guest.')
        command = [sys.executable, str(Path(__file__).resolve()), '--execute', '--worker',
                   '--expected-gpus', str(args.expected_gpus)]
        return subprocess.run(command, timeout=args.timeout_seconds).returncode
    except subprocess.TimeoutExpired:
        print('CUDA check timed out. Inspect current GPU/process state; no reset or retry was performed.', file=sys.stderr)
        return 124
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
