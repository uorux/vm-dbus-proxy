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
                             (key,[('multiple',('b',1)),('cursor_mode',('u',4))])))
        h.from_guest(select.data)
        forwarded=frame(h.upstream.sendall.call_args.args[0])
        self.assertEqual(dict(body(forwarded)[1]),{'multiple':('b',0),'cursor_mode':('u',2)})
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
    def test_spoofed_response_rejected(self):
        h=self.make_host(); h.requests['/r']=('/org/freedesktop/portal/desktop/session/x/y',':1.5')
        f=frame(message(4,12,{1:'/r',2:'org.freedesktop.portal.Request',3:'Response',7:':1.6',8:'ua{sv}'},(0,[])))
        with self.assertRaises(ValueError): h.from_host(f,[])

class GuestTests(unittest.TestCase):
    def test_unavailable_pipewire_returns_error(self):
        guest=Guest(Mock(),'/broker',Mock())
        f=frame(message(1,71,{1:PATH,2:SCREEN,3:'OpenPipeWireRemote',6:PORTAL,8:'oa{sv}'},('/org/freedesktop/portal/desktop/session/x/y',[])))
        guest.from_app(f,True)
        result=frame(guest.client.sendall.call_args.args[0]); self.assertEqual(result.message_type,3); self.assertEqual(result.fields[5],71)
    def test_pipewire_reply_has_fd_index_and_portal_sender(self):
        guest=Guest(Mock(),'/broker',Mock()); guest.deliver=Mock()
        key='/org/freedesktop/portal/desktop/session/x/y'
        guest.sessions[key]=Mock(pw=42,sender=':1.7')
        call=frame(message(1,71,{1:PATH,2:SCREEN,3:'OpenPipeWireRemote',6:PORTAL,8:'oa{sv}'},(key,[])))
        guest.from_app(call,True)
        data,fd=guest.deliver.call_args.args
        self.assertEqual(fd,42)
        # Parse FD-bearing wire data using a real disposable received FD.
        import os
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
