#!/usr/bin/env python3
import asyncio, aiohttp
import random


host = '127.0.0.1'
port = 5053
headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}
upstreams = ['https://1.1.1.1/dns-query', 'https://1.0.0.1/dns-query']
conns = []


def main():
	# Setup event loop and UDP server
	print('Starting UDP server listening on: %s#%d' % (host, port))
	loop = asyncio.get_event_loop()
	listen = loop.create_datagram_endpoint(DohProtocol, local_addr = (host, port))
	transport, protocol = loop.run_until_complete(listen)

	# Connect to upstream servers
	for upstream in upstreams:
		print('Connecting to upstream server: %s' % (upstream))
		conns.append(loop.run_until_complete(upstream_connect()))

	# Serve forever
	try:
		loop.run_forever()
	except (KeyboardInterrupt, SystemExit):
		pass

	# Close upstream connections
	for conn in conns:
		loop.run_until_complete(upstream_close(conn))

	# Close UDP server and event loop
	transport.close()
	loop.close()


class DohProtocol(asyncio.DatagramProtocol):
	"""
	DNS over HTTPS protocol to use with asyncio.
	"""

	def connection_made(self, transport):
		self.transport = transport

	def datagram_received(self, data, addr):
		# Schedule packet forwarding coroutine
		asyncio.ensure_future(self.forward_packet(data, addr))

	def connection_lost(self, exc):
		pass

	async def forward_packet(self, data, addr):
		# Select upstream server to forward to
		index = random.randrange(len(upstreams))

		# Await upstream forwarding coroutine
		data = await upstream_forward(upstreams[index], data, conns[index])

		# Send DNS packet to client
		self.transport.sendto(data, addr)


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
	async with conn.post(url, data=data) as response:
		if response.status != 200:
			print('%s (%d): IN %s, OUT %s' % (url, response.status, data, await response.read()))

		return await response.read()


async def upstream_close(conn):
	"""
	Close an upstream connection.

	Params:
		conn - aiohttp session object to close
	"""

	await conn.close()


if __name__ == '__main__':
	main()
