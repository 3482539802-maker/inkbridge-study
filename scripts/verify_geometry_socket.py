"""Real production C++ loopback transport with fragmented input and peer faults."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest

from geometry_socket import GeometrySocket, MAX_PACKET
from native_host_build import compile_native

ROOT=Path(__file__).resolve().parents[1]


class NativeSocketChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT/'output/test-runs').mkdir(parents=True, exist_ok=True)
        cls.temporary=tempfile.TemporaryDirectory(prefix='geometry-socket-',dir=ROOT/'output/test-runs')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.binary=compile_native(cls.temporary.name,[ROOT/'scripts/verify_geometry_socket.cpp',
            ROOT/'app/entry/src/main/cpp/geometry_socket.cpp'],'socket-check', ['ws2_32.lib'])

    def setUp(self):
        self.process=subprocess.Popen([str(self.binary)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.port=int(self.process.stdout.readline())
        self.client=GeometrySocket('a'*32,self.port)

    def tearDown(self):
        self.client.close()
        if self.process.poll() is None:
            self.process.communicate('stop\n',timeout=2)
        else:
            self.process.communicate(timeout=2)
        self.assertEqual(self.process.returncode,0)

    def test_persistent_requests_keep_packet_id_and_bound(self):
        connection=None
        for nonce in ('1'*32,'2'*32):
            packet,_=self.client.request(nonce)
            header,data=packet.split(b'\n',1)
            self.assertEqual(json.loads(header)['request_id'],nonce)
            self.assertEqual(data,bytes([42])*(128*1024))
            if connection is not None:self.assertIs(self.client.connection,connection)
            connection=self.client.connection

    def test_fragmented_request_and_wrong_session_rejected(self):
        with socket.create_connection(('127.0.0.1',self.port),timeout=1) as peer:
            peer.sendall(b'INKGEOM1 '+b'b'*32+b' '+b'1'*32+b'\n')
            self.assertEqual(peer.recv(1),b'')
        with socket.create_connection(('127.0.0.1',self.port),timeout=1) as peer:
            for chunk in (b'INK',b'GEOM1 ',b'a'*32,b' ',b'2'*32,b'\n'):peer.sendall(chunk)
            length=struct.unpack('!I',self.client_receive(peer,4))[0]
            packet=self.client_receive(peer,length)
            self.assertEqual(json.loads(packet.split(b'\n')[0])['request_id'],'2'*32)

    @staticmethod
    def client_receive(peer,length):
        data=b''
        while len(data)<length:
            chunk=peer.recv(length-len(data))
            if not chunk:raise AssertionError('Truncated test response')
            data+=chunk
        return data

    def test_timeout_discards_late_response_before_reconnect(self):
        with self.assertRaises(TimeoutError):self.client.request('e'*32,timeout=.03)
        self.assertIsNone(self.client.connection)
        packet,_=self.client.request('3'*32)
        self.assertEqual(json.loads(packet.split(b'\n')[0])['request_id'],'3'*32)

    def test_cancellation_closes_connection(self):
        cancelled=threading.Event()
        timer=threading.Timer(.03,cancelled.set);timer.start()
        try:
            with self.assertRaisesRegex(RuntimeError,'cancelled'):
                self.client.request('f'*32,cancelled)
        finally:timer.join()
        self.assertIsNone(self.client.connection)

    def test_stop_wakes_native_wait_for_missing_video(self):
        peer=socket.create_connection(('127.0.0.1',self.port),timeout=1)
        try:
            peer.sendall(b'INKGEOM1 '+b'a'*32+b' '+b'f'*32+b'\n')
            time.sleep(.03)
            started=time.monotonic()
            self.process.communicate('stop\n',timeout=1)
            self.assertLess(time.monotonic()-started,1)
            self.assertEqual(peer.recv(1),b'')
        finally:peer.close()

    def test_oversized_native_payload_never_sent(self):
        with self.assertRaises(TimeoutError):self.client.request('d'*32,timeout=.1)
        self.assertIsNone(self.client.connection)


class PeerFaultChecks(unittest.TestCase):
    def fault(self,data):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen(1)
            def serve():
                with listener.accept()[0] as peer:
                    peer.recv(128);peer.sendall(data)
            worker=threading.Thread(target=serve);worker.start()
            client=GeometrySocket('a'*32,listener.getsockname()[1])
            try:
                with self.assertRaises((ValueError,ConnectionError)):client.request('b'*32)
                self.assertIsNone(client.connection)
            finally:client.close();worker.join(1)

    def test_truncated_and_oversized_packets_fail_closed(self):
        for data in (b'\0\0',struct.pack('!I',MAX_PACKET+1),struct.pack('!I',100)+b'short'):
            with self.subTest(data=data[:4]):self.fault(data)


if __name__=='__main__':unittest.main(verbosity=2)
