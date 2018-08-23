#!/usr/bin/env python3
import threading, queue
import socket, socketserver
import requests
import random


host = '127.0.0.1'
port = 5053
headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}
upstreams = ['https://1.1.1.1/dns-query', 'https://1.0.0.1/dns-query']
conns = []


def main():
	# Setup UDP server
	print('Starting UDP server listening on: %s#%d' % (host, port))
	server = UdpServer((host, port), UdpServerHandler)

	# Connect to upstream servers
	for upstream in upstreams:
		print('Connecting to upstream server: %s' % (upstream))
		conns.append(upstream_connect())

	# Serve forever
	try:
		server.serve_forever()
	except (KeyboardInterrupt, SystemExit):
		pass

	# Close upstream connections
	for conn in conns:
		upstream_close(conn)

	# Close UDP server
	server.shutdown()
	server.server_close()


class ThreadPoolMixIn(socketserver.ThreadingMixIn):
	"""
	Use a thread pool instead of a new thread to service every request.
	"""

	num_threads = 4
	allow_reuse_address = True

	def serve_forever(self):
		"""
		Handle one request at a time forever or until error.
		"""

		# Setup threadpool
		self.requests = queue.Queue(self.num_threads)

		for i in range(self.num_threads):
			t = threading.Thread(target=self.process_request_thread, daemon=True)
			t.start()

		# Server main loop
		while True:
			self.handle_request()

		self.server_close()

	def process_request_thread(self):
		"""
		Obtain request from queue instead of directly from server socket.
		"""

		while True:
			socketserver.ThreadingMixIn.process_request_thread(self, *self.requests.get())

	def handle_request(self):
		"""
		Collect requests and put them on the queue to be processed.
		"""

		try:
			request, client_address = self.get_request()
		except socket.error:
			return

		if self.verify_request(request, client_address):
			self.requests.put((request, client_address))


class UdpServer(socketserver.ThreadingMixIn, socketserver.UDPServer):
	pass


class UdpServerHandler(socketserver.BaseRequestHandler):
	def handle(self):
		# Accept requests from a client
		data, sock = self.request

		# Select upstream server to forward to
		index = random.randrange(len(upstreams))

		# Forward request to upstream server and get response
		data = upstream_forward(upstreams[index], data, conns[index])

		# Send response to client
		sock.sendto(data, self.client_address)


def upstream_connect():
	"""
	Create an upstream connection that will later be bound to a url.

	Returns:
		A requests session object
	"""

	# Create connection with default DNS message headers
	session = requests.Session()
	session.headers = headers
	return session


def upstream_forward(url, data, conn):
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

	return conn.post(url, data).content


def upstream_close(conn):
	"""
	Close an upstream connection.

	Params:
		conn - requests session object to close
	"""

	conn.close()


if __name__ == '__main__':
	main()
