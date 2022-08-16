all: ota

ota: *.c
		$(CC) -I/opt/homebrew/include/ -g 02_decompress.c ota.c -o ota -llzma

clean:
		rm -f ota
