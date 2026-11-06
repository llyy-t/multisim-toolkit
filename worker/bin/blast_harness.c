/* blast_harness.c -- decompress a PKWare (DCL) stream with Mark Adler's blast.c
 *
 * Usage: blast_harness <input> <output>
 *
 * Exists to decode the compressed blocks inside a Multisim .ms14 file: each
 * block holds PKWare DCL "implode" data, which zlib/bz2/lzma cannot read.
 *
 * The whole input is handed to blast() up front, so the input callback is only
 * a stub for the "ran out of data" path.
 *
 * I/O goes through the Win32 API rather than stdio on purpose.  When this
 * process is started by the 32-bit Python guest interpreter, a plain
 * fopen(path, "wb") fails with EACCES for every path outside the DSH
 * workspace, while the very same fopen succeeds inside the interpreter.  The
 * CreateFile calls below do not have that problem.
 *
 * Two details matter for portability, because this gets run as a plain
 * command-line tool (skills, CI, shells) rather than only from Python:
 *
 *   1. Paths are tried as UTF-8 first and as ANSI (CP_ACP) second.  The caller
 *      writes UTF-8, so a Chinese path such as 设计3.ms14 must be decoded as
 *      UTF-8 rather than through the process ANSI code page.
 *   2. If CREATE_ALWAYS fails with a sharing violation, the existing file is
 *      removed and the create retried once.  A stale read-only or mapped
 *      output is otherwise indistinguishable from a permissions problem in the
 *      message the user sees.
 */

#include <stdio.h>
#include <stdlib.h>
#include <windows.h>

#include "blast.h"

static HANDLE g_out = INVALID_HANDLE_VALUE;

static unsigned in_fun(void *how, unsigned char **buf) {
    (void)how;
    *buf = NULL;
    return 0; /* all input was supplied up front */
}

static int out_fun(void *how, unsigned char *buf, unsigned len) {
    DWORD done = 0;
    (void)how;
    if (g_out == INVALID_HANDLE_VALUE)
        return 1;
    /* WriteFile takes at most 64 KiB per call on some drivers; loop to be safe. */
    while (done < len) {
        DWORD chunk = (DWORD)((len - done) > 65536u ? 65536u : (len - done));
        DWORD wrote = 0;
        if (!WriteFile(g_out, buf + done, chunk, &wrote, NULL) || wrote == 0)
            return 1;
        done += wrote;
    }
    return 0;
}

/* Open a path for reading, trying UTF-8 then the ANSI code page. */
static HANDLE open_read(const char *path) {
    wchar_t wide[32768];
    HANDLE handle;

    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, wide,
                            (int)(sizeof(wide) / sizeof(wide[0]))) > 0) {
        handle = CreateFileW(wide, GENERIC_READ,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
        if (handle != INVALID_HANDLE_VALUE)
            return handle;
    }
    return CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                       NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
}

/* Create a path for writing, trying UTF-8 then the ANSI code page, and
 * clearing a stale file once if the first attempt is blocked by it. */
static HANDLE create_write(const char *path) {
    wchar_t wide[32768];
    DWORD attempt;
    int wide_ok;

    wide_ok = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, wide,
                                  (int)(sizeof(wide) / sizeof(wide[0]))) > 0;

    for (attempt = 0; attempt < 2; attempt++) {
        HANDLE handle;

        if (wide_ok)
            handle = CreateFileW(wide, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                                 FILE_ATTRIBUTE_NORMAL, NULL);
        else
            handle = CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                                 FILE_ATTRIBUTE_NORMAL, NULL);
        if (handle != INVALID_HANDLE_VALUE)
            return handle;

        /* A leftover file may be read-only or still mapped.  Clear it once. */
        if (attempt == 0) {
            if (wide_ok)
                SetFileAttributesW(wide, FILE_ATTRIBUTE_NORMAL);
            else
                SetFileAttributesA(path, FILE_ATTRIBUTE_NORMAL);
            if (wide_ok)
                DeleteFileW(wide);
            else
                DeleteFileA(path);
        }
    }
    return INVALID_HANDLE_VALUE;
}

static unsigned char *read_all(const char *path, DWORD *out_len) {
    HANDLE file;
    LARGE_INTEGER size;
    DWORD got = 0;
    unsigned char *buffer;

    file = open_read(path);
    if (file == INVALID_HANDLE_VALUE) {
        fprintf(stderr, "cannot open input %s (error %lu)\n", path,
                (unsigned long)GetLastError());
        return NULL;
    }
    if (!GetFileSizeEx(file, &size) || size.QuadPart <= 0) {
        fprintf(stderr, "input is empty\n");
        CloseHandle(file);
        return NULL;
    }
    if (size.QuadPart > 512u * 1024u * 1024u) {
        fprintf(stderr, "input too large: %lld bytes\n", (long long)size.QuadPart);
        CloseHandle(file);
        return NULL;
    }
    buffer = (unsigned char *)malloc((size_t)size.QuadPart);
    if (buffer == NULL) {
        fprintf(stderr, "out of memory\n");
        CloseHandle(file);
        return NULL;
    }
    if (!ReadFile(file, buffer, (DWORD)size.QuadPart, &got, NULL) ||
        got != (DWORD)size.QuadPart) {
        fprintf(stderr, "short read\n");
        free(buffer);
        CloseHandle(file);
        return NULL;
    }
    CloseHandle(file);
    *out_len = got;
    return buffer;
}

int main(int argc, char **argv) {
    unsigned char *inbuf;
    DWORD inlen = 0;
    int err;
    unsigned left;
    unsigned char *next;

    if (argc != 3) {
        fprintf(stderr, "usage: %s <input> <output>\n", argv[0]);
        return 2;
    }
    inbuf = read_all(argv[1], &inlen);
    if (inbuf == NULL)
        return 2;

    g_out = create_write(argv[2]);
    if (g_out == INVALID_HANDLE_VALUE) {
        fprintf(stderr, "cannot open output %s (error %lu)\n", argv[2],
                (unsigned long)GetLastError());
        free(inbuf);
        return 2;
    }

    left = (unsigned)inlen;
    next = inbuf;
    err = blast(in_fun, NULL, out_fun, NULL, &left, &next);

    FlushFileBuffers(g_out);
    {
        LARGE_INTEGER pos;
        pos.QuadPart = 0;
        pos.LowPart = GetFileSize(g_out, (LPDWORD)&pos.HighPart);
        CloseHandle(g_out);
        g_out = INVALID_HANDLE_VALUE;
        fprintf(stderr, "blast=%d wrote=%lld unused_input=%u\n", err,
                (long long)pos.QuadPart, left);
    }
    free(inbuf);
    return err == 0 ? 0 : 1;
}
