# doh-forwarder
DNS over HTTPS forwarder.

doh-async.py seems to be best for my use case.

### Requirements
These libraries are necessary for the proper execution of the program. Program behaviour without these prerequisites installed is undefined.
- aiohttp library  
	sudo apt install python3-pip -y && sudo pip3 install aiohttp

### Suggestions
The base program can be enhanced automatically by installing optional libraries. These are not required and the base program will run perfectly fine without them.
- uvloop library  
	minor performance increase:  
	sudo apt install python3-pip -y && sudo pip3 install uvloop
- aiodns library  
	slight performance increase:  
	sudo apt install python3-pip -y && sudo pip3 install aiodns

### TODO
- [x] Add install/uninstall script (install as a service via systemd)
- [x] Add argument parsing for common configurables
- [x] Add TCP resolving in addition to UDP resolving
- [ ] Add DNS packet parsing to print human-readable packets to log
- [ ] Add upstream server metrics and heuristics
- [ ] Use exceptions to detect connection errors and attempt to reconnect
