#!/usr/bin/env python3
import socket
import requests


host = '127.0.0.1'
port = 5053
upstreams = ['https://cloudflare-dns.com/dns-query']
headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}


def main():
	print('Starting UDP server listening on: %s#%d' % (host, port))
	sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	sock.bind((host, port))

	print('Connecting to upstream server: %s' % (upstreams[0]))

	try:
		while True:
			# Accept requets from a client
			data, addr = sock.recvfrom(576)

			# Forward request to upstream server and get response
			data = upstream_forward(upstreams[0], data)

			# Send response to client
			sock.sendto(data, addr)

	except (KeyboardInterrupt, SystemExit):
		pass

	sock.shutdown(socket.SHUT_RDWR)
	sock.close()


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
