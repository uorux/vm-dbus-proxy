import unittest
import struct
from unittest.mock import Mock, patch
from vm_dbus_proxy.capture_wire import message, body, replace_fields
from vm_dbus_proxy.capture_proxy import frame, Host, Guest, SCREEN, PORTAL, PATH

class CodecTests(unittest.TestCase):
    def test_nested_start_response_roundtrip(self):
        values=(0,[('streams',('a(ua{sv})',[(47,[('size',('(ii)',(1920,1080))),('source_type',('u',1))])])),('restore_token',('s','abc'))])
        for endian in ('<','>'):
            f=frame(message(4,9,{1:'/test',2:'org.test',3:'Response',8:'ua{sv}'},values,endian=endian))
            self.assertEqual(body(f),values)
            self.assertEqual(body(frame(replace_fields(f,{**f.fields,7:':1.2'}))),values)
    def test_truncated_body_rejected(self):
        data=message(2,2,{5:1,8:'s'},('hello',))
        with self.assertRaises(ValueError): frame(data[:-1])

class HostTests(unittest.TestCase):
    def make_host(self): return Host(Mock(),'/broker',Mock())
    def test_start_rewrites_node_after_guest_ready(self):
        h=self.make_host()
        call=frame(message(1,83,{1:PATH,2:SCREEN,3:'Start',6:PORTAL,8:'osa{sv}'},('/org/freedesktop/portal/desktop/session/1_2/abc','',[])))
        h.send_upstream(call)
        reply=frame(message(2,12,{5:1,7:':1.5',8:'o'},('/org/freedesktop/portal/desktop/request/1_2/a',)))
        h.from_host(reply,[])
        delivered=frame(h.tunnel.send.call_args.args[1]); self.assertEqual(delivered.fields[5],83)
        response=frame(message(4,13,{1:'/org/freedesktop/portal/desktop/request/1_2/a',2:'org.freedesktop.portal.Request',3:'Response',7:':1.5',8:'ua{sv}'},(0,[('streams',('a(ua{sv})',[(72,[])]))])))
        with patch('threading.Timer'):
            h.from_host(response,[])
        internal=frame(h.upstream.sendall.call_args.args[0]); self.assertEqual(internal.fields[3],'OpenPipeWireRemote')
        self.assertEqual(body(internal)[0],body(call)[0])
        h.ready(b'{"session":"/org/freedesktop/portal/desktop/session/1_2/abc","node":101}')
        result=frame(h.tunnel.send.call_args.args[1]); self.assertEqual(body(result)[1][0][1][1][0][0],101)
        self.assertEqual(result.fields[7],':1.5')
    def test_unique_portal_destination_is_intercepted_after_owner_lookup(self):
        h=self.make_host()
        bus='org.freedesktop.DBus'
        lookup=frame(message(1,80,{1:'/org/freedesktop/DBus',2:bus,3:'GetNameOwner',6:bus,8:'s'},(PORTAL,)))
        h.from_guest(lookup.data)
        h.from_host(frame(message(2,10,{5:1,7:bus,8:'s'},(':1.55',))),[])
        self.assertEqual(h.portal_sender,':1.55')
        key='/org/freedesktop/portal/desktop/session/1_2/abc'
        request='/org/freedesktop/portal/desktop/request/1_2/start'
        select=frame(message(1,81,{1:PATH,2:SCREEN,3:'SelectSources',6:':1.55',8:'oa{sv}'},
                             (key,[('multiple',('b',1)),('cursor_mode',('u',4)),('persist_mode',('u',2)),('restore_token',('s','t'))])))
        h.from_guest(select.data)
        forwarded=frame(h.upstream.sendall.call_args.args[0])
        # One stream, cursor embedded, and always the host picker (no restore).
        self.assertEqual(dict(body(forwarded)[1]),{'multiple':('b',0),'cursor_mode':('u',2),'persist_mode':('u',0)})
        call=frame(message(1,82,{1:PATH,2:SCREEN,3:'Start',6:':1.55',8:'osa{sv}'},(key,'',[])))
        h.from_guest(call.data)
        h.from_host(frame(message(2,11,{5:3,7:':1.55',8:'o'},(request,))),[])
        response=frame(message(4,12,{1:request,2:'org.freedesktop.portal.Request',3:'Response',7:':1.55',8:'ua{sv}'},
                               (0,[('streams',('a(ua{sv})',[(72,[])]))])))
        h.tunnel.send.reset_mock()
        with patch('threading.Timer'):
            h.from_host(response,[])
        h.tunnel.send.assert_not_called()
        self.assertIn(key,h.held)
        self.assertEqual(frame(h.upstream.sendall.call_args.args[0]).fields[3],'OpenPipeWireRemote')
        h.close()

    def test_owner_tracking_requires_bus_sender(self):
        h=self.make_host(); bus='org.freedesktop.DBus'
        lookup=frame(message(1,80,{1:'/org/freedesktop/DBus',2:bus,3:'GetNameOwner',6:bus,8:'s'},(PORTAL,)))
        h.from_guest(lookup.data)
        h.from_host(frame(message(2,10,{5:1,7:':1.666',8:'s'},(':1.666',))),[])
        self.assertIsNone(h.portal_sender)
        for sender,owner in [(':1.666',None),(bus,':1.55')]:
            changed=frame(message(4,11,{1:'/org/freedesktop/DBus',2:bus,3:'NameOwnerChanged',7:sender,8:'sss'},
                                  (PORTAL,'',':1.55')))
            h.from_host(changed,[])
            self.assertEqual(h.portal_sender,owner)
        lost=frame(message(4,12,{1:'/org/freedesktop/DBus',2:bus,3:'NameOwnerChanged',7:bus,8:'sss'},
                           (PORTAL,':1.55','')))
        h.from_host(lost,[])
        self.assertIsNone(h.portal_sender)

    def test_unexpected_host_fd_reply_is_error_and_all_fds_close(self):
        import os
        from helpers import stream_pair, feed
        from vm_dbus_proxy.capture_proxy import receive_fds
        h=self.make_host()
        h.send_upstream(frame(message(1,55,{1:'/test',3:'GetFD'})))
        response=message(2,8,{5:1,9:1,8:'h'},(0,))
        a,b=stream_pair(); r,w=os.pipe()
        try:
            feed(b,response,[r]); received,descriptors=receive_fds(b)
            duplicate=descriptors[0]
            h.from_host(received,descriptors)
            returned=frame(h.tunnel.send.call_args.args[1])
            self.assertEqual(returned.message_type,3)
            self.assertEqual(returned.fields[5],55)
            self.assertEqual(returned.fd_count,0)
            for fd in (r,duplicate):
                with self.assertRaises(OSError): os.fstat(fd)
        finally: os.close(w); a.close(); b.close()
    def test_late_timeout_does_not_close_ready_session(self):
        h=self.make_host(); session=Mock(); h.sessions['/session']=session
        h.fail('/session',timeout=True)
        session.close.assert_not_called(); h.tunnel.send.assert_not_called()
        self.assertIn('/session',h.sessions)
    def test_shutdown_cancels_pending_start(self):
        h=self.make_host(); timer=Mock(); h.timers['/session']=timer
        h.held['/session']=Mock()
        h.close()
        timer.cancel.assert_called_once(); self.assertEqual(h.held,{})
        h.fail('/session',timeout=True); h.tunnel.send.assert_not_called()
    def test_unrelated_reply_body_is_opaque(self):
        h=self.make_host(); call=frame(message(1,25,{1:'/test',3:'Whatever'})); h.send_upstream(call)
        response=frame(message(2,9,{5:1,8:'s'},('value',)))
        h.from_host(response,[])
        result=frame(h.tunnel.send.call_args.args[1]); self.assertEqual(result.fields[5],25); self.assertEqual(body(result),('value',))
    def test_multistream_fails_explicitly(self):
        h=self.make_host(); h.requests['/r']=('/org/freedesktop/portal/desktop/session/x/y',':1.5')
        f=frame(message(4,12,{1:'/r',2:'org.freedesktop.portal.Request',3:'Response',7:':1.5',8:'ua{sv}'},(0,[('streams',('a(ua{sv})',[(1,[]),(2,[])]))])))
        h.from_host(f,[]); self.assertEqual(body(frame(h.tunnel.send.call_args.args[1])),(2,[]))
        close=frame(h.upstream.sendall.call_args.args[0])
        self.assertEqual((close.fields[1],close.fields[3],close.flags&1),('/org/freedesktop/portal/desktop/session/x/y','Close',1))
    def test_spoofed_response_is_not_an_approval(self):
        h=self.make_host(); key='/org/freedesktop/portal/desktop/session/x/y'; h.requests['/r']=(key,':1.5')
        streams=[('streams',('a(ua{sv})',[(72,[])]))]
        f=frame(message(4,12,{1:'/r',2:'org.freedesktop.portal.Request',3:'Response',7:':1.6',8:'ua{sv}'},(0,streams)))
        h.from_host(f,[])
        # Passed through untouched; no remote opened; the real answer still counts.
        h.tunnel.send.assert_called_once_with(b'D',f.data); h.upstream.sendall.assert_not_called()
        self.assertIn('/r',h.requests)
        real=frame(message(4,13,{1:'/r',2:'org.freedesktop.portal.Request',3:'Response',7:':1.5',8:'ua{sv}'},(0,streams)))
        with patch('threading.Timer'): h.from_host(real,[])
        self.assertEqual(frame(h.upstream.sendall.call_args.args[0]).fields[3],'OpenPipeWireRemote')
        self.assertNotIn('/r',h.requests); h.close()
    def test_outstanding_calls_do_not_retain_messages(self):
        h=self.make_host()
        big=frame(message(1,7,{1:'/x',3:'Big',6:':1.9',8:'ay'},([0]*(1<<20),)))
        h.send_upstream(big)
        (entry,)=h.pending.values()
        self.assertFalse(any(isinstance(v,(bytes,bytearray)) or hasattr(v,'data') for v in entry))
        self.assertLess(len(repr(entry)),200)
    def test_start_tracking_is_bounded(self):
        from vm_dbus_proxy.capture_proxy import MAX_REQUESTS
        h=self.make_host()
        for i in range(MAX_REQUESTS+5):
            call=frame(message(1,100+i,{1:PATH,2:SCREEN,3:'Start',6:PORTAL,8:'osa{sv}'},('/org/freedesktop/portal/desktop/session/1/s%d'%i,'',[])))
            h.send_upstream(call)
            serial=frame(h.upstream.sendall.call_args.args[0]).serial
            h.from_host(frame(message(2,10+i,{5:serial,7:':1.5',8:'o'},('/org/freedesktop/portal/desktop/request/1/r%d'%i,))),[])
        self.assertEqual(len(h.requests),MAX_REQUESTS)
        self.assertIn('/org/freedesktop/portal/desktop/request/1/r%d'%(MAX_REQUESTS+4),h.requests)
    def test_host_fd_call_is_answered_upstream(self):
        import os
        from helpers import stream_pair, feed
        from vm_dbus_proxy.capture_proxy import receive_fds
        h=self.make_host()
        call=message(1,31,{1:'/app',3:'TakeFD',7:':1.9',8:'h',9:1},(0,))
        a,b=stream_pair(); r,w=os.pipe()
        try:
            feed(b,call,[r]); received,descriptors=receive_fds(b)
            h.from_host(received,descriptors)
            h.tunnel.send.assert_not_called()
            error=frame(h.upstream.sendall.call_args.args[0])
            self.assertEqual((error.message_type,error.fields[5],error.fields[6]),(3,31,':1.9'))
        finally: os.close(w); a.close(); b.close()
    def test_malformed_tunnel_records_are_protocol_errors(self):
        from vm_dbus_proxy.capture_proxy import record
        from vm_dbus_proxy.protocol import ProtocolError
        for payload in (b'[]',b'{}',b'{"session":["x"]}',b'{"session":{}}'):
            with self.subTest(payload=payload), self.assertRaises(ProtocolError): record(payload)
        h=self.make_host()
        with self.assertRaises(ProtocolError): h.ready(b'{"session":"/s","node":"7"}')
        g=Guest(Mock(),'/broker',Mock())
        with self.assertRaises(ProtocolError): g.start(b'{"session":"/org/freedesktop/portal/desktop/session/a/b","sender":5}')

class GuestTests(unittest.TestCase):
    def test_unavailable_pipewire_returns_error(self):
        guest=Guest(Mock(),'/broker',Mock())
        f=frame(message(1,71,{1:PATH,2:SCREEN,3:'OpenPipeWireRemote',6:PORTAL,8:'oa{sv}'},('/org/freedesktop/portal/desktop/session/x/y',[])))
        guest.from_app(f,True)
        result=frame(guest.client.sendall.call_args.args[0]); self.assertEqual(result.message_type,3); self.assertEqual(result.fields[5],71)
    def test_pipewire_reply_has_fd_index_and_portal_sender(self):
        import os
        guest=Guest(Mock(),'/broker',Mock()); seen=[]
        guest.deliver=Mock(side_effect=lambda data,fd: seen.append(os.fstat(fd).st_ino))
        key='/org/freedesktop/portal/desktop/session/x/y'
        pr,pw=os.pipe()
        guest.sessions[key]=Mock(pw=pr,sender=':1.7')
        call=frame(message(1,71,{1:PATH,2:SCREEN,3:'OpenPipeWireRemote',6:PORTAL,8:'oa{sv}'},(key,[])))
        try:
            guest.from_app(call,True)
            data,fd=guest.deliver.call_args.args
            # A private duplicate of the session's remote, closed after sending.
            self.assertNotEqual(fd,pr); self.assertEqual(seen,[os.fstat(pr).st_ino])
            with self.assertRaises(OSError): os.fstat(fd)
        finally: os.close(pr); os.close(pw)
        # Parse FD-bearing wire data using a real disposable received FD.
        from helpers import stream_pair,feed
        from vm_dbus_proxy.protocol import read_frame
        a,b=stream_pair(); r,w=os.pipe()
        try:
            feed(b,data,[r]); result=read_frame(b)
            self.assertEqual(result.fields[7],':1.7')
            self.assertEqual(result.fields[5],71); self.assertEqual(body(result),(0,))
            self.assertEqual(result.fd_count,1)
        finally: os.close(w); a.close(); b.close()
    def test_unrelated_method_passes_unchanged(self):
        guest=Guest(Mock(),'/broker',Mock())
        f=frame(message(1,71,{1:'/x',2:'org.example',3:'OpenPipeWireRemote',6:'org.example',8:'s'},('anything',)))
        guest.from_app(f,True); guest.tunnel.send.assert_called_once_with(b'D',f.data)

if __name__=='__main__': unittest.main()

class TunnelIntegrationTests(unittest.TestCase):
    def test_auth_and_calls_through_both_adapters(self):
        import threading
        from helpers import stream_pair, line
        from vm_dbus_proxy.capture_proxy import run_connection
        from vm_dbus_proxy.protocol import read_frame
        app,front=stream_pair(); guest_back,host_front=stream_pair(); host_back,bus=stream_pair()
        threads=[threading.Thread(target=run_connection,args=('guest',front,guest_back,'/unused'),daemon=True),threading.Thread(target=run_connection,args=('host',host_front,host_back,'/unused'),daemon=True)]
        for t in threads: t.start()
        try:
            app.sendall(b'\0AUTH EXTERNAL 31303031\r\n')
            self.assertEqual(bus.recv(1),b'\0'); self.assertEqual(line(bus),b'AUTH EXTERNAL 31303031\r\n')
            bus.sendall(b'OK 0123456789abcdef0123456789abcdef\r\n')
            self.assertTrue(line(app).startswith(b'OK '))
            app.sendall(b'NEGOTIATE_UNIX_FD\r\n'); self.assertEqual(line(app),b'AGREE_UNIX_FD\r\n')
            app.sendall(b'BEGIN\r\n'); self.assertEqual(line(bus),b'NEGOTIATE_UNIX_FD\r\n')
            bus.sendall(b'AGREE_UNIX_FD\r\n'); self.assertEqual(line(bus),b'BEGIN\r\n')
            app.sendall(message(1,987,{1:'/test',3:'Test'}))
            forwarded=read_frame(bus); self.assertEqual(forwarded.serial,1)
            bus.sendall(message(2,21,{5:forwarded.serial,8:'s'},('works',)))
            returned=read_frame(app); self.assertEqual(returned.fields[5],987); self.assertEqual(body(returned),('works',))
        finally:
            app.close(); bus.close()
            for t in threads:
                t.join(3); self.assertFalse(t.is_alive())
