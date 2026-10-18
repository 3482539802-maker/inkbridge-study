"""Bounded transient geometry packets over an HDC-forwarded loopback socket."""
import re
import socket
import struct
import time

MAX_PACKET = 8 * 1024 * 1024 + 8192


class GeometrySocket:
    def __init__(self, token, port=18769):
        if not isinstance(token, str) or re.fullmatch('[0-9a-f]{32}', token) is None:
            raise ValueError('Invalid geometry session token')
        self.token, self.port, self.connection = token, port, None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    @staticmethod
    def check(deadline, cancelled):
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError('Geometry request cancelled')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Geometry packet deadline expired')
        return min(.2, remaining)

    def receive(self, length, deadline, cancelled):
        data = bytearray()
        while len(data) < length:
            self.connection.settimeout(self.check(deadline, cancelled))
            try:
                chunk = self.connection.recv(min(65536, length-len(data)))
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError('Geometry connection closed mid-packet')
            data.extend(chunk)
        return bytes(data)

    def request(self, nonce, cancelled=None, timeout=5):
        if not isinstance(nonce, str) or re.fullmatch('[0-9a-f]{32}', nonce) is None:
            raise ValueError('Invalid geometry request identity')
        if not 0 < timeout <= 5:
            raise ValueError('Bounded geometry deadline required')
        deadline = time.monotonic() + timeout
        try:
            self.check(deadline, cancelled)
            if self.connection is None:
                self.connection = socket.create_connection(('127.0.0.1', self.port), timeout=min(.3, timeout))
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection.settimeout(self.check(deadline, cancelled))
            self.connection.sendall(f'INKGEOM1 {self.token} {nonce}\n'.encode('ascii'))
            sent = time.monotonic()
            length, = struct.unpack('!I', self.receive(4, deadline, cancelled))
            if not 0 < length <= MAX_PACKET:
                raise ValueError('Invalid bounded geometry packet length')
            return self.receive(length, deadline, cancelled), sent
        except BaseException:
            self.close()
            raise
