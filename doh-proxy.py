#!/usr/bin/env python3

import sys
import ssl
import http.client
import asyncio

host = '127.0.0.1'
port = 5353
upstreams = ['https://1.1.1.1/dns-query', 'https://1.0.0.1/dns-query']
headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}

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
		self.transport = transport
		self.upstream = upstreams[0]
		print('Connecting to upstream server: %s' % (self.upstream))
		self.conn = upstream_connect(self.upstream)

	def datagram_received(self, data, addr):
		data = upstream_forward(self.upstream, self.conn, data)
		self.transport.sendto(data, addr)

	def connection_lost(self, exc):
		upstream_close(self.upstream, self.conn)


def upstream_connect(url):
	ctx = ssl.SSLContext(ssl.PROTOCOL_TLS)
	conn = http.client.HTTPSConnection('cloudflare-dns.com', context = ctx)
	return conn

def upstream_forward(url, conn, data):
	conn.request('POST', url, data, headers)
	data = conn.getresponse().read()
	return data

def upstream_close(url, conn):
	conn.close()

if __name__ == '__main__':
	main()
