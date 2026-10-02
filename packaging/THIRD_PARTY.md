# Included runtime and notices

The Linux distribution includes CPython, PyInstaller's bootloader, the official MCP Python SDK and its locked dependencies, and the Ubuntu OpenSSH client with non-glibc dependencies. Distribution notices are in `licenses/`; package versions are fixed in `packaging/requirements.txt` in the source repository.

CPython uses the PSF license; MCP uses MIT; PyInstaller uses GPL with its bootloader exception allowing application distribution; OpenSSH carries BSD-style and other notices in the supplied openssh-client copyright. OpenSSL, Kerberos, zlib, SELinux and PCRE notices are included. Glibc and the system loader remain supplied by the Linux OS.

No user's private keys, SSH configuration, known_hosts, tokens, local logs or server credentials are included in this release. No deployment definitions are bundled.
