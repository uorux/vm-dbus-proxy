/* Exercise untrusted control decoding without GPU or PipeWire daemon. */
#include <pipewire/pipewire.h>
#include <sys/socket.h>
static struct pw_buffer *test_dequeue(struct pw_stream *stream);
static int test_queue(struct pw_stream *stream, struct pw_buffer *buffer);
static ssize_t test_send(int fd, const void *data, size_t length, int flags);
#define pw_stream_dequeue_buffer test_dequeue
#define pw_stream_queue_buffer test_queue
#define send test_send
#define main capture_program_main
#include "vm-capture.c"
#undef main
#include <assert.h>

static struct pw_buffer *dequeued[32];
static unsigned dequeue_count, dequeue_next, returned, frames;
static struct packet last_frame;
static struct pw_buffer *test_dequeue(struct pw_stream *stream) {
    (void)stream;
    return dequeue_next<dequeue_count?dequeued[dequeue_next++]:NULL;
}
static int test_queue(struct pw_stream *stream, struct pw_buffer *buffer) {
    (void)stream;assert(buffer);returned++;return 0;
}
static ssize_t test_send(int fd, const void *data, size_t length, int flags) {
    (void)fd;(void)flags;assert(length==sizeof(struct packet));
    struct packet p;memcpy(&p,data,length);
    if(le32toh(p.op)==FRAME) { frames++;last_frame=p; }
    return length;
}

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
    /* Exercise the actual SPA DONT_FIXATE parser and the producer's two
     * negotiation callbacks, with no live daemon or GPU required. */
    a=fresh();a.guest=false;
    unsigned char storage[512];struct spa_pod_builder builder=SPA_POD_BUILDER_INIT(storage,sizeof storage);
    struct spa_pod_frame object,choice;
    spa_pod_builder_push_object(&builder,&object,SPA_TYPE_OBJECT_Format,SPA_PARAM_Format);
    spa_pod_builder_add(&builder,
        SPA_FORMAT_mediaType,SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype,SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format,SPA_POD_Id(SPA_VIDEO_FORMAT_BGRx),
        SPA_FORMAT_VIDEO_size,SPA_POD_Rectangle(&SPA_RECTANGLE(1920,1080)),0);
    spa_pod_builder_prop(&builder,SPA_FORMAT_VIDEO_modifier,SPA_POD_PROP_FLAG_MANDATORY|SPA_POD_PROP_FLAG_DONT_FIXATE);
    spa_pod_builder_push_choice(&builder,&choice,SPA_CHOICE_Enum,0);
    spa_pod_builder_long(&builder,0);spa_pod_builder_long(&builder,0);spa_pod_builder_long(&builder,123);
    spa_pod_builder_pop(&builder,&choice);
    const struct spa_pod *offer=spa_pod_builder_pop(&builder,&object);
    struct spa_video_info_raw f={0};
    assert(spa_format_video_raw_parse(offer,&f)>=0);
    assert(f.flags & SPA_VIDEO_FLAG_MODIFIER_FIXATION_REQUIRED);
    assert(!accept_format(&a,&f) && !a.configured && !a.stopping);
    f.flags &= ~SPA_VIDEO_FLAG_MODIFIER_FIXATION_REQUIRED;f.modifier=123;
    assert(accept_format(&a,&f) && a.format.modifier==123 && !a.stopping);
    f.modifier=456;assert(accept_format(&a,&f)); /* still no exported buffers */
    a.count=1;assert(accept_format(&a,&f)); /* repeated identical callback */
    f.modifier=789;assert(!accept_format(&a,&f) && a.stopping);
    pw_main_loop_destroy(a.loop);
    a=fresh();receive_packet(&a,config(1920,1080));
    f.modifier=123;assert(!accept_format(&a,&f) && a.stopping); /* guest import immutable */
    pw_main_loop_destroy(a.loop);
    /* Reproduce xdph's four-buffer bootstrap: recycle all frames until the
     * guest links, then reserve the remaining buffers for producer/graph scheduling.
     * Exercise process(), FLOW and DONE together, not just the limit predicate. */
    a=fresh();a.guest=false;a.ready=true;a.count=4;
    a.format=(struct spa_video_info_raw){.size=SPA_RECTANGLE(1920,1080)};
    struct spa_chunk chunks[4];struct spa_data datas[4];
    struct spa_buffer buffers[4];struct pw_buffer pw_buffers[4];
    for(unsigned i=0;i<4;i++) {
        chunks[i]=(struct spa_chunk){.stride=7680,.size=7680*1080};
        datas[i]=(struct spa_data){.chunk=&chunks[i]};
        buffers[i]=(struct spa_buffer){.n_datas=1,.datas=&datas[i]};
        pw_buffers[i]=(struct pw_buffer){.buffer=&buffers[i]};
        a.slots[i].pw=dequeued[i]=&pw_buffers[i];
        a.slots[i].imported.strides[0]=7680;
    }
    dequeue_count=4;dequeue_next=returned=frames=0;process(&a);
    assert(!a.stopping && returned==4 && frames==0 && a.sequence==0);
    p=(struct packet){.magic=htole32(0x31504356),.op=htole32(FLOW),.count=htole32(1)};
    receive_packet(&a,p);assert(a.consumer_active && !a.stopping);
    dequeue_next=returned=0;process(&a);
    assert(!a.stopping && returned==3 && frames==1 && a.sequence==1);
    assert(!a.slots[3].sent_ns);
    dequeued[0]=&pw_buffers[3];dequeue_count=1;dequeue_next=0;process(&a);
    assert(returned==4 && frames==1); /* unacknowledged slots stay held */
    struct packet done={.magic=htole32(0x31504356),.op=htole32(DONE),.slot=0,.seq=htole64(1)};
    receive_packet(&a,done);assert(!a.stopping && returned==5 && !a.slots[0].sent_ns);
    dequeued[0]=&pw_buffers[0];dequeue_next=0;process(&a);
    assert(frames==2 && le64toh(last_frame.seq)==2 && le32toh(last_frame.slot)==0);
    p.count=0;receive_packet(&a,p);assert(!a.consumer_active);
    dequeued[0]=&pw_buffers[3];dequeue_next=0;process(&a);
    assert(frames==2 && returned==6 && !a.stopping);
    p.count=htole32(2);receive_packet(&a,p);assert(a.stopping);
    pw_main_loop_destroy(a.loop);
    puts("capture control validation: passed");pw_deinit();return 0;
}
