#!/usr/bin/env python3
import asyncio, aiohttp
import logging, random
import argparse, struct

# Attempt to use uvloop if installed for extra performance
try:
	import uvloop
	asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
	pass

# Handle command line arguments
parser = argparse.ArgumentParser()
parser.add_argument('-a', '--listen-address', default='127.0.0.1',
					help='address to listen on for DNS over HTTPS server (default: %(default)s)')
parser.add_argument('-p', '--listen-port', type=int, default=53,
					help='port to listen on for DNS over HTTPS server (default: %(default)s)')
parser.add_argument('-u', '--upstreams', nargs='+', default=['https://1.1.1.1/dns-query', 'https://1.0.0.1/dns-query'],
					help='upstream servers to forward DNS queryies and requests to (default: %(default)s)')
parser.add_argument('-t', '--tcp', action='store_true', default=False,
					help='serve TCP based queries and requests along with UDP (default: %(default)s)')
args = parser.parse_args()

host = args.listen_address
port = args.listen_port
upstreams = args.upstreams

headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}
conns = []


def main():
	# Setup logging
	logging.basicConfig(level='INFO', format='[%(levelname)s] %(message)s')

	# Setup event loop
	loop = asyncio.get_event_loop()

	# Setup UDP server
	logging.info('Starting UDP server listening on: %s#%d' % (host, port))
	udp_listen = loop.create_datagram_endpoint(UdpDohProtocol, local_addr = (host, port))
	udp, protocol = loop.run_until_complete(udp_listen)

	# Setup TCP server
	if args.tcp:
		logging.info('Starting TCP server listening on %s#%d' % (host, port))
		tcp_listen = loop.create_server(TcpDohProtocol, host, port)
		tcp = loop.run_until_complete(tcp_listen)

	# Connect to upstream servers
	for upstream in upstreams:
		logging.info('Connecting to upstream server: %s' % (upstream))
		conns.append(loop.run_until_complete(upstream_connect()))

	# Serve forever
	try:
		loop.run_forever()
	except (KeyboardInterrupt, SystemExit):
		pass

	# Close upstream connections
	for conn in conns:
		loop.run_until_complete(upstream_close(conn))

	# Close listening servers and event loop
	udp.close()
	if args.tcp:
		tcp.close()

	loop.close()


class UdpDohProtocol(asyncio.DatagramProtocol):
	"""
	DNS over HTTPS UDP protocol to use with asyncio.
	"""

	def connection_made(self, transport):
		self.transport = transport

	def datagram_received(self, data, addr):
		# Schedule packet forwarding coroutine
		asyncio.ensure_future(self.forward_packet(data, addr))

	def error_received(self, exc):
		logging.warning('Minor transport error')

	async def forward_packet(self, data, addr):
		# Select upstream server to forward to
		index = random.randrange(len(upstreams))

		# Await upstream forwarding coroutine
		data = await upstream_forward(upstreams[index], data, conns[index])

		# Send DNS packet to client
		self.transport.sendto(data, addr)


class TcpDohProtocol(asyncio.Protocol):
	"""
	DNS over HTTPS TCP protocol to use with asyncio.
	"""

	def connection_made(self, transport):
		self.transport = transport

	def data_received(self, data):
		# Schedule packet forwarding coroutine
		asyncio.ensure_future(self.forward_packet(data))

	def eof_received(self):
		if self.transport.can_write_eof():
			self.transport.write_eof()

	def connection_lost(self, exc):
		self.transport.close()

	async def forward_packet(self, data):
		# Select upstream server to forward to
		index = random.randrange(len(upstreams))

		# Await upstream forwarding coroutine
		data = await upstream_forward(upstreams[index], data[2:], conns[index])

		# Send DNS packet to client
		if self.transport.is_reading():
			self.transport.write(struct.pack('! H', len(data)) + data)
		else:
			logging.warning('Client not reading, aborting transport')
			self.transport.abort()


async def upstream_connect():
	"""
	Create an upstream connection that will later be bound to a url.

	Returns:
		A aiohttp session object
	"""

	# Create connection with default DNS message headers
	return aiohttp.ClientSession(headers=headers)


async def upstream_forward(url, data, conn):
	"""
	Send a DNS request over HTTPS using POST method.

	Params:
		url  - url to forward queries to
		data - normal DNS packet data to forward
		conn - HTTPS connection to upstream DNS server

	Returns:
		A normal DNS response packet from upstream server

	Notes:
		Using DNS over HTTPS POST format as described here:
		https://tools.ietf.org/html/draft-ietf-doh-dns-over-https-12
		https://developers.cloudflare.com/1.1.1.1/dns-over-https/wireformat/
	"""

	# Await upstream response
	while True:
		try:
			async with conn.post(url, data=data) as response:
				if response.status == 200:
					return await response.read()

				logging.warning('%s (%d): IN %s, OUT %s' % (url, response.status, data, await response.read()))
		except aiohttp.ClientConnectionError:
			logging.exception('Connection error with upstream server: %s', url)
			return b''


async def upstream_close(conn):
	"""
	Close an upstream connection.

	Params:
		conn - aiohttp session object to close
	"""

	await conn.close()


if __name__ == '__main__':
	main()
