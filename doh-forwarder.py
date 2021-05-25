#!/usr/bin/env python3
import argparse
import array
import asyncio as aio
import base64
import itertools
import logging
import random
import statistics
from abc import ABCMeta, abstractmethod
from asyncio import DatagramTransport, Lock, StreamReader, StreamWriter, Task
from types import TracebackType
from typing import (ClassVar, Iterable, Iterator, List, Optional, Sequence,
                    Set, SupportsFloat, Tuple, Type, Union)

import httpx

DEFAULT_LISTEN_ADDRESSES = \
[
	'127.0.0.1',
	'::1',
]

DEFAULT_LISTEN_PORTS = \
[
	5053,
]

DEFAULT_UPSTREAMS = \
[
	'https://1.1.1.2:443/dns-query',
	'https://1.0.0.2:443/dns-query',
	'https://[2606:4700:4700::1112]:443/dns-query',
	'https://[2606:4700:4700::1002]:443/dns-query',
]


async def main(args) -> None:
	# Setup event loop
	loop = aio.get_running_loop()

	# Setup DNS resolver to cache/forward queries and answers
	async with AsyncDnsResolver(args.upstreams, AsyncDohUpstreamContext) as resolver:
		transports = []

		# Setup listening transports
		for addr in args.listen_address:
			for port in args.listen_port:
				# Setup UDP server
				logging.info('Starting UDP server listening on %s#%d' % (addr, port))
				udp, _ = await loop.create_datagram_endpoint(lambda: UdpResolverProtocol(resolver), local_addr=(addr, port))
				transports.append(udp)

				# Setup TCP server
				if args.tcp:
					logging.info('Starting TCP server listening on %s#%d' % (addr, port))
					tcp = await aio.start_server(TcpResolverProtocol(resolver).ahandle_peer, addr, port)
					transports.append(tcp)

		# Serve forever
		try:
			while True:
				await aio.sleep(3600)
				logging.info(resolver.get_stats())

		except (KeyboardInterrupt, SystemExit):
			pass

		logging.info('Shutting down DNS over HTTPS forwarder')

		wait_closers = []
		for transport in transports:
			transport.close()
			if hasattr(transport, 'wait_closed'):
				wait_closers.append(aio.create_task(transport.wait_closed()))

		await aio.wait(wait_closers)

	await aio.sleep(0.3)


class AsyncDnsUpstreamContext(metaclass=ABCMeta):
	"""A base class used to manage upstream DNS server connections and metadata."""

	RTT_WINDOW_SIZE: ClassVar[int] = 10

	def __init__(self, host: str) -> None:
		self.host = host
		self.queries = 0
		self.answers = 0
		self._rtts = array.array('d', [0.0])
		self._rtts_index: Iterator[int] = itertools.cycle(range(self.RTT_WINDOW_SIZE))

	async def __aenter__(self) -> 'AsyncDnsUpstreamContext':
		return self

	async def __aexit__(self,
		exc_type: Optional[Type[BaseException]],
		exc_val: Optional[BaseException],
		exc_tb: Optional[TracebackType]) -> None:
		await self.aclose()

	@property
	def avg_rtt(self) -> float:
		"""The average rtt or latency (in seconds) for DNS requests to this upstream DNS server."""
		return statistics.fmean(self._rtts)

	def add_rtt_sample(self, rtt: SupportsFloat) -> None:
		"""Add a new rtt sample to help compute the average rtt for this upstream DNS server."""
		i = next(self._rtts_index)
		self._rtts[i:i+1] = array.array('d', [float(rtt)])

	def get_stats(self) -> str:
		"""Returns a formatted string of statistics for this upstream server."""
		return f'{self.host} (rtt: {self.avg_rtt:.3f} s, queries: {self.queries}, answers: {self.answers})'

	@abstractmethod
	async def aforward_query(self, query: bytes) -> bytes:
		"""Resolve a DNS query via forwarding to a upstream DNS server.

		Params:
			query - A wireformat DNS query packet.

		Returns:
			A wireformat DNS answer packet.

		Notes:
			This coroutine is be safely cancellable. That is, even if the
			coroutine is cancelled it still leaves any internal state
			it uses in a consistent and usable state.
		"""
		...

	@abstractmethod
	async def aclose(self) -> None:
		"""Close any open connections to the upstream DNS server."""
		...


class AsyncDohUpstreamContext(AsyncDnsUpstreamContext):
	"""A class used to manage upstream DoH server connections and metadata."""

	SESSION_LIMITS: ClassVar[httpx.Limits] = httpx.Limits(max_keepalive_connections=1, max_connections=3, keepalive_expiry=60.0)
	SESSION_TIMEOUTS: ClassVar[httpx.Timeout] = httpx.Timeout(None)

	def __init__(self, url: str) -> None:
		super().__init__(url)
		self.session = httpx.AsyncClient(
			limits=self.SESSION_LIMITS,
			timeout=self.SESSION_TIMEOUTS,
			headers={'accept': 'application/dns-message'},
			http2=True)

	async def aforward_post(self, query: bytes) -> Tuple[bytes, float]:
		"""Resolve a DNS query via forwarding to a upstream DoH server (POST).

		Params:
			query - A wireformat DNS query packet.

		Returns:
			A wireformat DNS answer packet and rtt sample.

		Notes:
			Using DNS over HTTPS POST format as described here:
			https://datatracker.ietf.org/doc/html/rfc8484
			https://developers.cloudflare.com/1.1.1.1/dns-over-https/wireformat/
		"""
		# Send HTTP request to upstream DoH server and wait for the response
		response = await aio.shield(
			self.session.post(
				self.host,
				headers={'content-type': 'application/dns-message'},
				content=query))

		# Parse HTTP response
		response.raise_for_status()
		answer = response.read()
		rtt = response.elapsed.total_seconds()

		# Return the DNS answer
		return (answer, rtt)

	async def aforward_get(self, query: bytes) -> Tuple[bytes, float]:
		"""Resolve a DNS query via forwarding to a upstream DoH server (GET).

		Params:
			query - A wireformat DNS query packet.

		Returns:
			A wireformat DNS answer packet and rtt sample.

		Notes:
			Using DNS over HTTPS GET format as described here:
			https://datatracker.ietf.org/doc/html/rfc8484
			https://developers.cloudflare.com/1.1.1.1/dns-over-https/wireformat/
		"""
		# Encode DNS query into url
		url = ''.join([self.host, '?dns=', base64.urlsafe_b64encode(query).rstrip(b'=').decode()])

		# Send HTTP request to upstream DoH server and wait for the response
		response = await aio.shield(self.session.get(url))

		# Parse HTTP response
		response.raise_for_status()
		answer = response.read()
		rtt = response.elapsed.total_seconds()

		# Return the DNS answer
		return (answer, rtt)

	async def aforward_query(self, query: bytes) -> bytes:
		self.queries += 1

		query = memoryview(query)
		qid = query[:2]

		# Forward the DNS query to the upstream DoH server
		try:
			logging.debug(f'Sending query {qid.hex()} to {self.host} --->')
			answer, rtt = await self.aforward_post(b''.join([b'\0' * 2, query[2:]]))

			self.add_rtt_sample(rtt)
			self.answers += 1

			logging.debug(f'Receiving answer {qid.hex()} from {self.host} ({rtt}) <---')
			return b''.join([qid, memoryview(answer)[2:]])

		# Raise connection error
		except (httpx.NetworkError, httpx.RemoteProtocolError):
			raise ConnectionError(f'DNS query to DoH server {self.host} failed due to network errors')

		# Raise abnormal HTTP status codes
		except httpx.HTTPStatusError as exc:
			raise ConnectionError(f'received HTTP error status from DoH server {self.host} ({exc.response.status_code})')

	async def aclose(self) -> None:
		await self.session.aclose()


class AsyncDnsResolver:
	"""A class that manages upstream DNS server contexts and resolves DNS queries."""

	DEFAULT_QUERY_TIMEOUT: ClassVar[float] = 3.0

	def __init__(self, upstreams: Iterable[str], context_class: Type[AsyncDnsUpstreamContext]) -> None:
		self._upstreams = tuple(context_class(upstream) for upstream in upstreams)

		if not self._upstreams:
			raise ValueError('iterable of upstreams must have at least one entry')

	async def __aenter__(self) -> 'AsyncDnsResolver':
		return self

	async def __aexit__(self,
		exc_type: Optional[Type[BaseException]],
		exc_val: Optional[BaseException],
		exc_tb: Optional[TracebackType]) -> None:
		await self.aclose()

	@property
	def queries(self) -> int:
		return sum(upstream.queries for upstream in self._upstreams)

	@property
	def answers(self) -> int:
		return sum(upstream.answers for upstream in self._upstreams)

	@property
	def avg_rtt(self) -> float:
		return statistics.fmean(upstream.avg_rtt for upstream in self._upstreams)

	def get_stats(self) -> str:
		"""Returns a formatted string of statistics for this resolver."""
		return f'Statistics for resolver at 0x{id(self)} (avg_rtt: {self.avg_rtt:.3f} s, total_queries: {self.queries}, total_answers: {self.answers})'

	async def aresolve(self, query: bytes, timeout: float = DEFAULT_QUERY_TIMEOUT) -> bytes:
		"""Resolve a DNS query via forwarding to upstream DNS servers.

		Params:
			query - A wireformat DNS query packet.
			timeout - The maximum amount of time (in seconds) to wait for the receipt of the DNS answer packet.

		Returns:
			A wireformat DNS answer packet.
		"""
		# Forward the DNS query and return the DNS answer
		# (perform a staggered race and accept the earliest response)
		async def astaggered_resolution(upstreams: Sequence[AsyncDnsUpstreamContext], period: float) -> bytes:
			assert len(upstreams) > 0

			winner: Task = None
			racers: Set[Task] = set()
			errors: List[BaseException] = []

			# Wait for the first racer to finish and cleanup exceptions
			async def await_first_racer(timeout: float = None) -> bool:
				nonlocal winner
				nonlocal racers
				nonlocal errors

				done, racers = await aio.wait(racers, timeout=timeout, return_when=aio.FIRST_COMPLETED)

				for racer in done:
						error = racer.exception()

						if error is None:
							winner = racer
							break
						else:
							errors.append(error)

				return winner is not None

			try:
				for upstream in upstreams:
					racers.add(aio.create_task(upstream.aforward_query(query)))

					if await await_first_racer(period):
						return winner.result()

				while racers:
					if await await_first_racer():
						return winner.result()

			finally:
				for loser in racers:
					loser.cancel()

			def raise_multi_error(errors: Iterable[BaseException]) -> None:
				class MultiError(*frozenset(type(error) for error in errors)):
					pass

				raise MultiError

			assert len(errors) == len(upstreams)
			raise_multi_error(errors)

		# Weighted random shuffle the upstream servers by average latency
		k = len(self._upstreams)
		rtts = tuple(upstream.avg_rtt for upstream in self._upstreams)
		max_rtt = max(rtts)
		weights = (max_rtt - rtt + 0.001 for rtt in rtts)
		upstreams = random.choices(self._upstreams, weights=weights, k=k)
		period = (timeout / 2) / k if timeout is not None else 0.1

		# Forward the DNS query and return the DNS answer
		try:
			return await aio.wait_for(astaggered_resolution(upstreams, period), timeout)

		# Raise timeout error
		except aio.TimeoutError:
			raise TimeoutError(f'DNS query expired and was cancelled')

	async def aclose(self) -> None:
		"""Close all upstream DoH server connections."""
		for upstream in self._upstreams:
			await upstream.aclose()


class UdpResolverProtocol(aio.DatagramProtocol):
	"""Protocol for serving UDP DNS requests via a DnsResolver instance."""

	def __init__(self, resolver: AsyncDnsResolver) -> None:
		self.resolver = resolver
		self.buffer = []
		self.worker = None

	def connection_made(self, transport: DatagramTransport) -> None:
		self.transport = transport

	def datagram_received(self, data: bytes, peer: Tuple[str, int]) -> None:
		logging.debug(f'Got UDP DNS query from {peer}')

		# Add query to buffer
		self.buffer.append((peer, data))

		# Schedule query processing task if necessary
		if self.worker is None:
			self.worker = aio.create_task(self.ahandle_queries())

	async def ahandle_queries(self) -> None:
		while self.buffer:
			tasks = set(aio.create_task(self.ahandle_query(peer, query)) for peer, query in self.buffer)

			del self.buffer[:]

			while tasks:
				done, tasks = await aio.wait(tasks, timeout=0.05)

				for task in done:
					error = task.exception()

					if error is None:
						peer, answer = task.result()
						self.transport.sendto(answer, peer)
					else:
						logging.warning(f'UDP DNS query resolution encountered an error - {error!r}')

		self.worker = None

	async def ahandle_query(self, peer: Tuple[str, int], query: bytes) -> Tuple[Tuple[str, int], bytes]:
		return (peer, await self.resolver.aresolve(query))


class TcpResolverProtocol:
	"""Protocol for serving TCP DNS requests via a DnsResolver instance."""

	def __init__(self, resolver: AsyncDnsResolver) -> None:
		self.resolver = resolver

	async def ahandle_peer(self, reader: StreamReader, writer: StreamWriter) -> None:
		"""Read all DNS queries from the peer stream and schedule their resolution via a DnsResolver instance."""
		tasks: Union[List[Task], Set[Task]] = []
		wlock = aio.Lock()

		logging.debug(f'Got TCP DNS query stream from {writer.transport.get_extra_info("peername")}')

		while True:
			# Parse a DNS query packet off of the wire
			try:
				query_size = int.from_bytes(await reader.readexactly(2), 'big')
				query = await reader.readexactly(query_size)

			# Check if our peer has finished writing to the stream
			except aio.IncompleteReadError:
				break

			# Schedule the processing of the query
			tasks.append(aio.create_task(self.ahandle_query(writer, wlock, query)))

		# Wait for all scheduled query processing to finish
		while tasks:
			done, tasks = await aio.wait(tasks, return_when=aio.FIRST_COMPLETED)

			for task in done:
				error = task.exception()

				if error is not None:
					logging.warning(f'TCP DNS query resolution encountered an error - {error!r}')

		if not writer.is_closing():
			# Indicate we are done writing to the stream
			if writer.can_write_eof():
				writer.write_eof()

			# Close the stream
			writer.close()
			await writer.wait_closed()

	async def ahandle_query(self, writer: StreamWriter, wlock: Lock, query: bytes) -> None:
		"""Resolve a DNS query and write the DNS answer to the peer stream."""
		if writer.is_closing():
			return

		# Resolve DNS query
		answer = await self.resolver.aresolve(query)

		if writer.is_closing():
			return

		# Create the DNS answer packet
		answer_size = len(answer).to_bytes(2, 'big')
		answer = b''.join([answer_size, answer])

		# Write the DNS answer to the peer stream
		async with wlock:
			if writer.is_closing():
				return

			await writer.drain()

			if writer.is_closing():
				return

			writer.write(answer)


if __name__ == '__main__':
	# Handle command line arguments
	parser = argparse.ArgumentParser()
	parser.add_argument('-l', '--listen-address', nargs='+', default=DEFAULT_LISTEN_ADDRESSES,
						help='addresses to listen on for DNS over HTTPS requests (default: %(default)s)')
	parser.add_argument('-p', '--listen-port', nargs='+', type=int, default=DEFAULT_LISTEN_PORTS,
						help='ports to listen on for DNS over HTTPS requests (default: %(default)s)')
	parser.add_argument('-u', '--upstreams', nargs='+', default=DEFAULT_UPSTREAMS,
						help='upstream servers to forward DNS queries and requests to (default: %(default)s)')
	parser.add_argument('-t', '--tcp', action='store_true', default=False,
						help='serve TCP based queries and requests along with UDP (default: %(default)s)')
	parser.add_argument('-f', '--file', default=None,
						help='file to store logging output to (default: %(default)s)')
	parser.add_argument('-d', '--debug', action='store_true', default=False,
						help='enable debugging on the internal asyncio event loop (default: %(default)s)')
	args = parser.parse_args()

	# Setup logging
	log_level = 'DEBUG' if args.debug else 'INFO'
	logging.basicConfig(level=log_level, filename=args.file, filemode='w', format='(%(asctime)s)[%(levelname)s] %(message)s')
	logging.info('Starting DNS over HTTPS forwarder')
	logging.info('Args: %r' % (vars(args)))

	aio.run(main(args), debug=args.debug)
