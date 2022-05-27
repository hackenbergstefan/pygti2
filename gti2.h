#ifndef GTI2_H
#define GTI2_H

#include <stddef.h>
#include <stdint.h>

void gti2_dispatcher(void);
void gti2_read(uint8_t *buffer, size_t length);
void gti2_write(uint8_t *buffer, size_t length);

extern uint8_t gti2_memory[1024];

#endif
