#!/usr/bin/env python3
import threading, queue
import socket, socketserver
import requests


host = '127.0.0.1'
port = 5001
upstreams = ['https://cloudflare-dns.com/dns-query']
headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-udpwireformat'}


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
