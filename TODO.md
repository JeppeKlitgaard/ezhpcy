# TODO

## Iteration: refactor(forward): use forwarding instead of proxy

- [ ] Share one atomic private-file write between `tunnel/ssh_config.py::_write_private` and `ipc/descriptor.py::TunnelDescriptor.publish` (e.g. in `permissions.py`). Both write a temporary file and rename it; `_write_private` also restricts the Windows ACL (`icacls`) and writes `\n` line endings, `publish` writes through the audited directory's `dir_fd` on POSIX
- [ ] Store `ResourcesConfig.time_limit` as a `timedelta` instead of an `H:MM` string. Today the validator parses it only to reject bad input, and `time_limit_delta` parses it again on every access. Needs a serializer that writes `H:MM` back, since the value is dumped (e.g. into `ResolvedConfig.descriptor_digest`)
- [ ] Ensure that no blocking code runs in the tunnel server loop
  - Some may run briefly during startup of tunnel

## Iteration: Polish

- [ ] Documentation
  - [ ] Installation instructions
  - [ ] Setup instructions
  - [ ] Mermaid diagram of networking
- [ ] Need to be able to set password method outside of profile to have default for anonymous jobs. This also acts a fallback for all profiles
- [ ] Upgrade dependencies
- [ ] Upgrade pixi
- [ ] Upgrade OpenSSH server
- [ ] Do another LLM thorough code review
- [ ] Clean up unused scripts
- [ ] Ability to disable debug with --no-debug flag
- [ ] Distribute on Pypi

## Iteration: Next

- [ ] Consider `uvloop`/`winloop` and/or `aiofastnet`
- [ ] Resillience to dropped connection?
