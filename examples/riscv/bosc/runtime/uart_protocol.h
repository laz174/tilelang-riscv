/* Scheduler UART v1 records. Included on NH after host_puts/host_hex. */
#ifndef TILELANG_BOSC_UART_PROTOCOL_H
#define TILELANG_BOSC_UART_PROTOCOL_H

#include <stdint.h>

static void bosc_uart_start(void) {
  host_puts("@@FPGA_START {\"version\":1}\r\n");
}

/* Called only after NH drains RA logs and checks the console overflow flag. */
static void bosc_uart_exit(int status) {
  host_puts(status == 0 ? "@@FPGA_RESULT {\"version\":1,\"status\":\"exited\","
                          "\"app_exit_code\":0}\r\n"
                        : "@@FPGA_RESULT {\"version\":1,\"status\":\"exited\","
                          "\"app_exit_code\":1}\r\n");
}

static void bosc_uart_trap(uintptr_t cause, uintptr_t pc, uintptr_t value) {
  host_puts("@@FPGA_RESULT {\"version\":1,\"status\":\"trap\",\"mcause\":\"0x");
  host_hex(cause);
  host_puts("\",\"mepc\":\"0x");
  host_hex(pc);
  host_puts("\",\"mtval\":\"0x");
  host_hex(value);
  host_puts("\"}\r\n");
}

#endif
