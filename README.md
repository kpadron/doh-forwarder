# doh-forwarder
DNS over HTTPS forwarder.

doh-async.py seems to be best for my use case.

TODO:
- Add install/uninstall script (install as a service via systemd)
- Test uvloop performance
- Test aiodns performance
- Use exceptions to detect connection errors and attempt to reconnect
- Add argument parsing
- Add upstream server metrics and heuristics
