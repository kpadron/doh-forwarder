# doh-forwarder
DNS over HTTPS forwarder.

This program is a basic attempt at creating a DNS over HTTPS inline-proxy forwarder.
This means that it accepts standard UDP or TCP DNS packets and converts them to DoH HTTP requests.
Queries made by this program are encrypted using TLS schemes defined in the python standard library **ssl**.
The program can be configured with command line options to support a listening address and any non-standard ports.

This program is meant to be single-threaded and is based on the python standard library module **asyncio**.
Asynchronous HTTP requests are made over encrypted connections to upstream servers via required library **httpx**.
This allows for extra performance when many requests are received at once.
If TCP resolving is enabled extra threads may be spawned to accept connections on the listening socket.
Please note that this program was originally configured for operation with Cloudflare's public DNS servers and as such may contain specifics to that resolver.

**doh-forwarder.py** is the main program for this project and as such will have the most features implemented.
Other scripts in this repository represent different approaches to the same problem.

### Requirements
These libraries are necessary for the proper execution of the program.
Program behavior without these prerequisites installed is undefined.
- [httpx](https://github.com/encode/httpx/) required for asynchronous http requests
	> sudo apt install python3-pip -y && sudo pip3 install httpx[http2]

### Installation
This short guide assumes running on a 64-bit systemd based linux machine, other configurations are untested.

Intalling or reinstalling this program as a system service is as simple as running the **install.sh** script with super user permissions.

	chmod +x install.sh
	sudo ./install.sh doh-forwarder.py

This will place the unit service file in the proper directory and load the program to run immediately and on reboot.
Please modify the service file command line options as necessary before running the **install.sh** script:

	[Unit]
	Description=DNS over HTTPS stub forwarder
	After=syslog.target network-online.target

	[Service]
	ExecStart=/usr/local/bin/doh-forwarder -l a.b.c.d -p 5053 --tcp -u https://x.x.x.x/dns-query https://y.y.y.y/dns-query
	Restart=on-failure
	RestartSec=10
	KillMode=process

	[Install]
	WantedBy=multi-user.target

### Uninstallation
Uninstalling this program is as simple as running the **uninstall.sh** script with super user permissions.

	chmod +x uninstall.sh
	sudo ./uninstall.sh

This will undo all previous modifications done to your system as a result of running the **install.sh** script.

### TODO
- [x] Add install/uninstall script (install as a service via systemd)
- [x] Add argument parsing for common configurables
- [x] Add TCP resolving in addition to UDP resolving
- [] Use exceptions to detect connection errors and attempt to reconnect
