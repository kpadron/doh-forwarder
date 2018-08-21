#!/usr/bin/env python3
import asyncio
import requests


host = '127.0.0.1'
port = 5053
upstreams = ['https://cloudflare-dns.com/dns-query']
headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}


def main():
	print('Starting UDP server listening on: %s#%d' % (host, port))

	loop = asyncio.get_event_loop()
	listen = loop.create_datagram_endpoint(DohProtocol, local_addr = (host, port))
	transport, protocol = loop.run_until_complete(listen)

	print('Connecting to upstream server: %s' % (upstreams[0]))

	try:
		loop.run_forever()
	except (KeyboardInterrupt, SystemExit):
		pass

	transport.close()
	loop.close()


class DohProtocol:
	def connection_made(self, transport):
		self.loop = asyncio.get_event_loop()
		self.transport = transport
		self.upstream = upstreams[0]

	def datagram_received(self, data, addr):
		self.loop.create_task(self.forward_packet(data, addr))

	async def forward_packet(self, data, addr):
		data = await upstream_forward(self.upstream, self.conn, data)
		self.transport.sendto(data, addr)


async def upstream_forward(url, data):
	"""
	Send a DNS request over HTTPS using POST method.

	Params:
		url - url to forward queries to
		data - normal DNS packet data to forward

	Returns:
		normal DNS response packet from upstream server

	Notes:
		Using Cloudflare's DNS over HTTPS POST format as described here:
		https://developers.cloudflare.com/1.1.1.1/dns-over-https/wireformat/
	"""

	return requests.post(url, data, headers=headers).content


if __name__ == '__main__':
	main()
