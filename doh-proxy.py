#!/usr/bin/env python3

import sys
import ssl
import http.client
import urllib.parse
import asyncio

host = '127.0.0.1'
port = 5353
upstreams = ['https://cloudflare-dns.com/dns-query']
headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-udpwireformat'}


def main():
	loop = asyncio.get_event_loop()

	print('Starting UDP server: %s#%d' % (host, port))
	listen = loop.create_datagram_endpoint(DohProtocol, local_addr = (host, port))

	transport, protocol = loop.run_until_complete(listen)

	try:
		loop.run_forever()
	except KeyboardInterrupt:
		pass

	transport.close()
	loop.close()


class DohProtocol:
	def connection_made(self, transport):
		self.loop = asyncio.get_event_loop()
		self.transport = transport
		self.upstream = upstreams[0]
		print('Connecting to upstream server: %s' % (self.upstream))
		self.conn = upstream_connect(self.upstream)

	def datagram_received(self, data, addr):
		self.loop.create_task(self.forward_packet(data, addr))

	def connection_lost(self, exc):
		print('Disconnection from upstream server: %s' % (self.upstream))
		upstream_close(self.upstream, self.conn)

	async def forward_packet(self, data, addr):
		# data = await upstream_forward(self.upstream, self.conn, data)
		self.transport.sendto(data, addr)


def upstream_connect(url):
	"""
	Open secure connection to upstream server.

	Params:
		url - url of upstream server to connect to

	Returns:
		connection object for corresponding connection
	"""

	ctx = ssl.SSLContext(ssl.PROTOCOL_TLS)
	netloc = urllib.parse.urlparse(url)[1]
	conn = http.client.HTTPSConnection(netloc, context = ctx)
	return conn


async def upstream_forward(url, conn, data):
	"""
	Send a DNS request over HTTPS using POST method.

	Params:
		url - url to forward queries to
		conn - open https connection to the upstream server
		data - normal DNS packet data to forward

	Returns:
		normal DNS response packet from upstream server

	Notes:
		Using Cloudflare's DNS over HTTPS POST format as described here:
		https://developers.cloudflare.com/1.1.1.1/dns-over-https/wireformat/
	"""

	conn.request('POST', url, data, headers)
	data = conn.getresponse().read()
	return data


def upstream_close(url, conn):
	"""
	Close connection to upstream server.

	Params:
		url - url of upstream server to disconnect from
		conn - open https connection to the upstream server
	"""

	conn.close()


if __name__ == '__main__':
	main()
