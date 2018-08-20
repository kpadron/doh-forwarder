#!/usr/bin/env python3

import socket
import requests
import http.client
import urllib.parse

host = '127.0.0.1'
port = 5001
upstreams = ['https://cloudflare-dns.com/dns-query']
headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-udpwireformat'}

def main():
	print('Starting UDP server listening on: %s#%d' % (host, port))
	sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	sock.bind((host, port))

	print('Connecting to upstream server: %s' % (upstreams[0]))
	conn = upstream_connect()

	while True:
		try:
			# Accept requets from a client
			data, addr = sock.recvfrom(576)

			# Forward request to upstream server and get response
			data = upstream_forward(upstreams[0], data, conn)

			# Send response to client
			sock.sendto(data, addr)

		except KeyboardInterrupt:
			break

	sock.shutdown(socket.SHUT_RDWR)
	sock.close()

	upstream_close(conn)


def upstream_connect():
	"""
	Open secure connection to upstream server.

	Params:
		url - url of upstream server to connect to

	Returns:
		connection object for corresponding connection
	"""
	return requests.Session()


def upstream_forward(url, data, conn = None):
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

	if conn:
		return conn.post(url, data, headers=headers).content

	else:
		return requests.post(url, data, headers=headers).content


def upstream_close(conn):
	"""
	Close connection to upstream server.

	Params:
		conn - open https connection to the upstream server
	"""

	conn.close()


if __name__ == '__main__':
	main()
