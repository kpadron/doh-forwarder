#!/usr/bin/env python3
import socket
import threading, queue
import requests, random


host = '127.0.0.1'
port = 5053
headers = {'accept': 'application/dns-message', 'content-type': 'application/dns-message'}
upstreams = ['https://1.1.1.1/dns-query', 'https://1.0.0.1/dns-query']
conns = []

queue = queue.Queue()
sock_lock = threading.Lock()
workers = 4


def main():
	# Setup UDP server
	print('Starting UDP server listening on: %s#%d' % (host, port))
	sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	sock.bind((host, port))

	# Connect to upstream servers
	for upstream in upstreams:
		print('Connecting to upstream server: %s' % (upstream))
		conns.append(upstream_connect())

	# Serve forever
	try:
		# Sub threads are responsible for forwarding requests
		for _ in range(workers):
			threading.Thread(target=worker, args=(sock,), daemon=True).start()

		# Main thread is responsible for accepting requests
		while True:
			# Accept requests from a client
			data, addr = sock.recvfrom(4096)

			# Add request to queue
			queue.put((data, addr))
	except (KeyboardInterrupt, SystemExit):
		pass

	# Close upstream connections
	for conn in conns:
		upstream_close(conn)

	# Close UDP server
	sock.close()


def worker(sock):
	while True:
		# Retrieve request from the queue
		data, addr = queue.get()
		queue.task_done()

		# Select upstream server to forward to
		index = random.randrange(len(upstreams))

		# Forward request to upstream server and get response
		data = upstream_forward(upstreams[index], data, conns[index])

		# Send reponse to client
		with sock_lock:
			sock.sendto(data, addr)


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
