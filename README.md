# doh-forwarder
DNS over HTTPS forwarder.

This program is a basic attempt at creating a DNS over HTTPS inline-proxy forwarder. This means that it accepts standard UDP or TCP DNS packets and converts them to DoH HTTP requests. Queries made by this program are encrypted using schemes defined in the python standard ssl library. The program can be configured with command line options to support a listening address and any non-standard ports.

This program does not cache any queries that are resolved by the upstream DNS servers. This program is single threaded and based on the python standard library asyncio. Asynchronous HTTP requests are made over an encrypted connection to upstream servers. This allows for extra performance when many requests are received at once. If TCP resolving is enabled extra threads are spawned to accept connections on the listening socket. Please note that this program was originally configured for operation with cloudflare's public DNS servers and as such may contain specifics to that resolver.

**doh-async.py** is the main program for this project and as such will have the most features implemented. Other scripts in this repository represent different approaches to the same problem.

### Requirements
These libraries are necessary for the proper execution of the program. Program behavior without these prerequisites installed is undefined.
- aiohttp library https://github.com/aio-libs/aiohttp/  
	sudo apt install python3-pip -y && sudo pip3 install aiohttp


### Suggestions
The base program can be enhanced automatically by installing optional libraries. These are not required and the base program will run perfectly fine without them.
- uvloop library https://github.com/MagicStack/uvloop  
	minor performance increase:  
	sudo apt install python3-pip -y && sudo pip3 install uvloop
- aiodns library https://github.com/saghul/aiodns  
	slight performance increase:  
	sudo apt install python3-pip -y && sudo pip3 install aiodns

### Installation
This short guide assumes running on a 64-bit systemd based linux machine, other configurations are untested.

Intalling this program as a system service is as simple as running the **install.sh** script with super user permissions.

	chmod +x install.sh
	sudo ./install.sh

This will place the unit service file in the proper directory and load the program to run immediately and on reboot. Please modify the service file command line options as necessary before running the **install.sh** script:

	[Service]
	ExecStart=/usr/local/bin/doh-forwarder -a 192.168.1.56 -p 5053 --tcp -u https://example-upstream1.com/dns-query https://example-upstream2.com/dns-query
	Restart=on-failure
	RestartSec=10
	KillMode=process

### Uninstallation
Uninstalling this program is as simple as running the **uninstall.sh** script with super user permissions.

	chmod +x uninstall.sh
	sudo ./uninstall.sh

This will undo all previous modifications done to your system as a result of running **install.sh**.

### TODO
- [x] Add install/uninstall script (install as a service via systemd)
- [x] Add argument parsing for common configurables
- [x] Add TCP resolving in addition to UDP resolving
- [ ] Add DNS packet parsing to print human-readable packets to log
- [ ] Add EDNS0 client subnet disable to enhance privacy
- [ ] Add upstream server metrics and heuristics
- [ ] Use exceptions to detect connection errors and attempt to reconnect
