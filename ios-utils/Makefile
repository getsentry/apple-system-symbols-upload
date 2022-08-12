all: pbzx ota

pbzx:
		$(CC) -I/opt/homebrew/include/ 02_decompress.c pbzx.c -o pbzx -llzma

ota:
		$(CC) ota.c -o ota

clean:
		rm -f ota pbzx
