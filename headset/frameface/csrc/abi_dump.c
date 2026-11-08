/* SPDX-License-Identifier: MIT */
/*
 * abi_dump: print this system's usbfs ABI as one JSON object (FrameFace spec Appendix A.6).
 *
 * The selftest compares the output key by key with frameface.usbfs.abi_table(), which derives
 * the same values from the ctypes structs, so a kernel header that disagrees with the Python
 * layout is caught before the service touches the tracker. Build:
 *   gcc -std=c99 -O2 -Wall -Wextra -Werror -o abi_dump abi_dump.c
 */
#include <stdio.h>
#include <stddef.h>
#include <sys/ioctl.h>
#include <linux/usbdevice_fs.h>

#define UL(x) ((unsigned long)(x))

static void item(const char *key, unsigned long value, int last)
{
    printf("\"%s\":%lu%s", key, value, last ? "" : ",");
}

int main(void)
{
    printf("{\"sizes\":{");
    item("ctrltransfer", UL(sizeof(struct usbdevfs_ctrltransfer)), 0);
    item("setinterface", UL(sizeof(struct usbdevfs_setinterface)), 0);
    item("urb", UL(sizeof(struct usbdevfs_urb)), 0);
    item("disconnect_claim", UL(sizeof(struct usbdevfs_disconnect_claim)), 0);
    item("getdriver", UL(sizeof(struct usbdevfs_getdriver)), 1);

    printf("},\"offsets\":{");
    item("ctrltransfer.bRequestType", UL(offsetof(struct usbdevfs_ctrltransfer, bRequestType)), 0);
    item("ctrltransfer.bRequest", UL(offsetof(struct usbdevfs_ctrltransfer, bRequest)), 0);
    item("ctrltransfer.wValue", UL(offsetof(struct usbdevfs_ctrltransfer, wValue)), 0);
    item("ctrltransfer.wIndex", UL(offsetof(struct usbdevfs_ctrltransfer, wIndex)), 0);
    item("ctrltransfer.wLength", UL(offsetof(struct usbdevfs_ctrltransfer, wLength)), 0);
    item("ctrltransfer.timeout", UL(offsetof(struct usbdevfs_ctrltransfer, timeout)), 0);
    item("ctrltransfer.data", UL(offsetof(struct usbdevfs_ctrltransfer, data)), 0);
    item("setinterface.interface", UL(offsetof(struct usbdevfs_setinterface, interface)), 0);
    item("setinterface.altsetting", UL(offsetof(struct usbdevfs_setinterface, altsetting)), 0);
    item("urb.type", UL(offsetof(struct usbdevfs_urb, type)), 0);
    item("urb.endpoint", UL(offsetof(struct usbdevfs_urb, endpoint)), 0);
    item("urb.status", UL(offsetof(struct usbdevfs_urb, status)), 0);
    item("urb.flags", UL(offsetof(struct usbdevfs_urb, flags)), 0);
    item("urb.buffer", UL(offsetof(struct usbdevfs_urb, buffer)), 0);
    item("urb.buffer_length", UL(offsetof(struct usbdevfs_urb, buffer_length)), 0);
    item("urb.actual_length", UL(offsetof(struct usbdevfs_urb, actual_length)), 0);
    item("urb.start_frame", UL(offsetof(struct usbdevfs_urb, start_frame)), 0);
    /* number_of_packets shares an anonymous union with stream_id */
    item("urb.number_of_packets", UL(offsetof(struct usbdevfs_urb, number_of_packets)), 0);
    item("urb.error_count", UL(offsetof(struct usbdevfs_urb, error_count)), 0);
    item("urb.signr", UL(offsetof(struct usbdevfs_urb, signr)), 0);
    item("urb.usercontext", UL(offsetof(struct usbdevfs_urb, usercontext)), 0);
    item("disconnect_claim.interface", UL(offsetof(struct usbdevfs_disconnect_claim, interface)), 0);
    item("disconnect_claim.flags", UL(offsetof(struct usbdevfs_disconnect_claim, flags)), 0);
    item("disconnect_claim.driver", UL(offsetof(struct usbdevfs_disconnect_claim, driver)), 0);
    item("getdriver.interface", UL(offsetof(struct usbdevfs_getdriver, interface)), 0);
    item("getdriver.driver", UL(offsetof(struct usbdevfs_getdriver, driver)), 1);

    printf("},\"ioctl\":{");
    item("CONTROL", UL(USBDEVFS_CONTROL), 0);
    item("SETINTERFACE", UL(USBDEVFS_SETINTERFACE), 0);
    item("SETCONFIGURATION", UL(USBDEVFS_SETCONFIGURATION), 0);
    item("GETDRIVER", UL(USBDEVFS_GETDRIVER), 0);
    item("SUBMITURB", UL(USBDEVFS_SUBMITURB), 0);
    item("DISCARDURB", UL(USBDEVFS_DISCARDURB), 0);
    item("REAPURB", UL(USBDEVFS_REAPURB), 0);
    item("REAPURBNDELAY", UL(USBDEVFS_REAPURBNDELAY), 0);
    item("CLAIMINTERFACE", UL(USBDEVFS_CLAIMINTERFACE), 0);
    item("RELEASEINTERFACE", UL(USBDEVFS_RELEASEINTERFACE), 0);
    item("RESET", UL(USBDEVFS_RESET), 0);
    item("CLEAR_HALT", UL(USBDEVFS_CLEAR_HALT), 0);
    item("GET_CAPABILITIES", UL(USBDEVFS_GET_CAPABILITIES), 0);
    item("DISCONNECT_CLAIM", UL(USBDEVFS_DISCONNECT_CLAIM), 0);
    item("GET_SPEED", UL(USBDEVFS_GET_SPEED), 1);

    printf("},\"consts\":{");
    item("URB_TYPE_BULK", UL(USBDEVFS_URB_TYPE_BULK), 0);
    item("DISCONNECT_CLAIM_IF_DRIVER", UL(USBDEVFS_DISCONNECT_CLAIM_IF_DRIVER), 0);
    item("DISCONNECT_CLAIM_EXCEPT_DRIVER", UL(USBDEVFS_DISCONNECT_CLAIM_EXCEPT_DRIVER), 0);
    item("FIONREAD", UL(FIONREAD), 1);
    printf("}}\n");
    return 0;
}
