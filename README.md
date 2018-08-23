# doh-forwarder
DNS over HTTPS forwarder.

doh-async.py seems to be best for my use case.

### Requirements
- aiohttp library
	* sudo apt install python3-pip -y && sudo pip3 install aiohttp

### Suggestions
- uvloop library
	* minor performance increase:
	sudo apt install python3-pip -y && sudo pip3 install uvloop
- aiodns library
	* slight performance increase:
	sudo apt install python3-pip -y && sudo pip3 install aiodns

### TODO
- [x] Add install/uninstall script (install as a service via systemd)
- [x] Add argument parsing for common configurables
- [x] Add TCP resolving in addition to UDP resolving
- [ ] Add DNS packet parsing to print human-readable packets to log
- [ ] Add upstream server metrics and heuristics
- [ ] Use exceptions to detect connection errors and attempt to reconnect
