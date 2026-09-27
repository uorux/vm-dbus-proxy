/* Exercise untrusted control decoding without GPU or PipeWire daemon. */
#define main capture_program_main
#include "vm-capture.c"
#undef main
#include <assert.h>

static struct app fresh(void) {
    struct app a={.guest=true};
    a.loop=pw_main_loop_new(NULL);
    assert(a.loop);
    return a;
}
static struct packet config(uint32_t width,uint32_t height) {
    return (struct packet){.magic=htole32(0x31504356),.op=htole32(CONFIG),
        .width=htole32(width),.height=htole32(height),.format=htole32(SPA_VIDEO_FORMAT_BGRx),
        .modifier=htole64(DRM_FORMAT_MOD_LINEAR)};
}
int main(void) {
    pw_init(NULL,NULL);
    struct app a=fresh();receive_packet(&a,config(1920,1080));
    assert(a.configured && !a.stopping && a.format.size.width==1920 && a.format.size.height==1080);
    receive_packet(&a,config(1920,1080));assert(a.stopping);pw_main_loop_destroy(a.loop);
    uint32_t invalid[][2]={{0,1080},{1920,0},{8193,1},{1,8193},{UINT32_MAX,UINT32_MAX}};
    for(unsigned i=0;i<sizeof invalid/sizeof invalid[0];i++) {
        a=fresh();receive_packet(&a,config(invalid[i][0],invalid[i][1]));
        assert(a.stopping && !a.configured);pw_main_loop_destroy(a.loop);
    }
    a=fresh();struct packet p=config(1920,1080);p.magic=0;receive_packet(&a,p);
    assert(a.stopping);pw_main_loop_destroy(a.loop);
    a=fresh();p=config(1920,1080);p.format=htole32(SPA_VIDEO_FORMAT_I420);receive_packet(&a,p);
    assert(a.stopping);pw_main_loop_destroy(a.loop);
    a=fresh();p=config(1920,1080);p.op=htole32(READY);p.count=htole32(1);receive_packet(&a,p);
    assert(a.stopping);pw_main_loop_destroy(a.loop);
    a=fresh();p=config(1920,1080);p.op=htole32(FRAME);p.slot=htole32(32);receive_packet(&a,p);
    assert(a.stopping);pw_main_loop_destroy(a.loop);
    a=fresh();a.guest=false;p=config(1920,1080);p.op=htole32(DONE);receive_packet(&a,p);
    assert(a.stopping);pw_main_loop_destroy(a.loop);
    /* Frames can queue before the guest links; validate their crop bounds and
     * sequence without calling any PipeWire stream method. */
    a=fresh();receive_packet(&a,config(1920,1080));a.stream=(struct pw_stream *)1;a.count=1;
    p=config(1920,1080);p.op=htole32(FRAME);p.seq=htole64(1);p.format=0;
    receive_packet(&a,p);assert(!a.stopping && a.slots[0].pending && a.slots[0].crop.size.width==1920);
    receive_packet(&a,p);assert(a.stopping);pw_main_loop_destroy(a.loop);
    a=fresh();receive_packet(&a,config(1920,1080));a.stream=(struct pw_stream *)1;a.count=1;
    p.id=htole32(UINT32_MAX);receive_packet(&a,p);assert(a.stopping);pw_main_loop_destroy(a.loop);
    a=fresh();receive_packet(&a,config(1920,1080));a.stream=(struct pw_stream *)1;a.count=1;
    p.id=0;p.format=htole32(8);receive_packet(&a,p);assert(a.stopping);pw_main_loop_destroy(a.loop);
    puts("capture control validation: passed");pw_deinit();return 0;
}
