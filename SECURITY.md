# Security

Use [GitHub private vulnerability reporting](https://github.com/mrabhiram/oci-a100-kvm/security/advisories/new) to report security issues. Include the affected version, impact and steps to reproduce. Do not put exploit details or credentials in public issues.

Keep private keys, OCI configuration, host inventories and generated state outside the repository. Use trusted host administrators and limit network access to the guests.

This project has no production security certification. Functional GPU tests do not establish isolation against hostile tenants. See [validation](docs/validation.md) for the tested scope.
