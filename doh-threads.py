#!/usr/bin/env python3
import threading
import socket, socketserver
import requests


host = '127.0.0.1'
port = 5053
upstreams = ['https://cloudflare-dns.com/dns-query']
headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}


def main():
	print('Starting UDP server listening on: %s#%d' % (host, port))
	server = UdpServer((host, port), UdpServerHandler)

	print('Connecting to upstream server: %s' % (upstreams[0]))

	try:
		server.serve_forever()
	except (KeyboardInterrupt, SystemExit):
		pass

	server.shutdown()
	server.server_close()


class UdpServer(socketserver.ThreadingMixIn, socketserver.UDPServer):
	pass


class UdpServerHandler(socketserver.BaseRequestHandler):
	def handle(self):
		data, sock = self.request

		data = upstream_forward(upstreams[0], data)

		sock.sendto(data, self.client_address)


def upstream_forward(url, data):
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
