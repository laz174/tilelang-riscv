/* Host harness: real patched NR console reporting, with platform I/O stubs. */
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define NR_CONSOLE_CAPACITY 64u
#define NR_CONSOLE_APPEND_ONLY 1

static void fence(void) {}
static void flush(const volatile void *pointer) { (void)pointer; }
static void invalidate(const volatile void *pointer) { (void)pointer; }
static void host_puts(const char *text) { fputs(text, stdout); }
static void host_hex(uint64_t value) { printf("%016" PRIX64, value); }
static void uart_putc(char value) { putchar((unsigned char)value); }
static void pump_input(void) {}

#include "uart_protocol.h"

/* The patched console reporter calls the protocol helpers above. */
#include "nr_console.inc"

int main(int argc, char **argv) {
  if (argc != 2)
    return 2;
  nr_console_init();
  bosc_uart_start();
  if (strcmp(argv[1], "trap") == 0) {
    bosc_uart_trap(2, 0x80000040, 0xffffffff);
    return 0;
  }
  int status;
  if (strcmp(argv[1], "pass") == 0) {
    status = 1;
  } else if (strcmp(argv[1], "fail") == 0) {
    status = 2;
  } else if (strcmp(argv[1], "overflow") == 0) {
    status = 1;
    for (unsigned i = 0; i <= NR_CONSOLE_CAPACITY; ++i)
      write_serial('x');
  } else {
    return 2;
  }
  /* Exercise forwarding before NH emits the terminal record. */
  (void)nr_console_poll_running(0);
  host_puts("\r\n");
  nr_console_report((uint8_t)status);
  return ferror(stdout) ? 1 : 0;
}
