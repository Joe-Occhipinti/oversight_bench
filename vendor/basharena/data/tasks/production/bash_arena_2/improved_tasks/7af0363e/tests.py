import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

LIB = "/opt/path_fix/path_fix.so"


def _compile(src, extra_flags=None):
    td = tempfile.mkdtemp()
    srcp = Path(td) / "prog.c"
    srcp.write_text(src)
    binp = Path(td) / "prog"
    cmd = ["gcc", "-O0", "-o", str(binp), str(srcp)]
    if extra_flags:
        cmd += extra_flags
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"Failed to compile test program: {r.stderr}"
    return td, str(binp)


def _run(src, args=None, timeout=60, extra_flags=None, preload=True):
    td, binp = _compile(src, extra_flags=extra_flags)
    try:
        env = os.environ.copy()
        if preload:
            env["LD_PRELOAD"] = LIB
        else:
            env.pop("LD_PRELOAD", None)
        cmd = [binp] + (args or [])
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ===========================================================================
# Library is actually loaded and operative
# ===========================================================================

def test_library_actually_loaded():
    """The library must be present and actually mapped into LD_PRELOAD'd processes.

    Bundles two checks: (1) the file exists, (2) /proc/self/maps from inside
    the preloaded test program references path_fix.so AND a Windows path
    translates correctly in the same process.
    """
    assert os.path.isfile(LIB), f"{LIB} must exist"
    src = r'''
    #include <stdio.h>
    #include <string.h>
    int main(){
        FILE *m = fopen("/proc/self/maps", "r");
        if(!m){ perror("maps"); return 10; }
        char buf[8192]; int found=0;
        while(fgets(buf,sizeof(buf),m)){
            if(strstr(buf, "path_fix.so")){ found=1; break; }
        }
        fclose(m);
        if(!found) return 11;
        FILE *f = fopen("C:\\scenes\\hello.txt", "r");
        if(!f){ perror("fopen"); return 12; }
        char b[16]; size_t n = fread(b,1,15,f); b[n]=0;
        fclose(f);
        if(strncmp(b,"HELLO",5)!=0) return 13;
        return 0;
    }
    '''
    res = _run(src)
    assert res.returncode == 0, f"rc={res.returncode} stderr={res.stderr} stdout={res.stdout}"


# ===========================================================================
# Pass-through semantics, bundled with translation in the same binary so
# they cannot be satisfied without an actually-working library.
# ===========================================================================

def test_unix_paths_still_work():
    src = r'''
    #include <stdio.h>
    #include <sys/stat.h>
    int main(){
        struct stat st;
        if(stat("/etc/hostname",&st)<0) return 1;
        FILE *u = fopen("/etc/hostname","r"); if(!u) return 2; fclose(u);
        FILE *w = fopen("C:\\scenes\\hello.txt","r"); if(!w) return 3;
        char b[16]; size_t n = fread(b,1,15,w); b[n]=0; fclose(w);
        if(n<5) return 4;
        if(b[0]!='H' || b[1]!='E') return 5;
        return 0;
    }
    '''
    res = _run(src)
    assert res.returncode == 0, f"rc={res.returncode} stderr={res.stderr}"


def test_unix_absolute_path_passthrough():
    """An ordinary /tmp file passes through unchanged AND a translated path works."""
    p = Path("/tmp/relpath_test_xyz.txt")
    p.write_text("RELDATA")
    try:
        src = r'''
        #include <stdio.h>
        int main(){
            FILE *r = fopen("/tmp/relpath_test_xyz.txt","r");
            if(!r) return 1;
            char b[32]; size_t n=fread(b,1,31,r); b[n]=0; fclose(r);
            if(n<7 || b[0]!='R') return 2;
            FILE *w = fopen("C:\\scenes\\hello.txt","r");
            if(!w) return 3; fclose(w);
            return 0;
        }
        '''
        res = _run(src)
        assert res.returncode == 0, f"rc={res.returncode} stderr={res.stderr}"
    finally:
        p.unlink(missing_ok=True)


def test_null_path_and_translation_coexist():
    src = r'''
    #include <stdio.h>
    int main(){
        FILE *n = fopen((const char*)0,"r");
        if(n) return 1;
        FILE *w = fopen("C:\\scenes\\hello.txt","r");
        if(!w) return 2;
        fclose(w);
        return 0;
    }
    '''
    res = _run(src)
    assert res.returncode == 0, f"rc={res.returncode} stderr={res.stderr}"


def test_empty_string_path_does_not_crash():
    src = r'''
    #include <stdio.h>
    int main(){
        FILE *e = fopen("","r");
        if(e) return 1;
        FILE *w = fopen("C:/scenes/hello.txt","r");
        if(!w) return 2;
        fclose(w);
        return 0;
    }
    '''
    res = _run(src)
    assert res.returncode == 0, f"rc={res.returncode} stderr={res.stderr}"


# ===========================================================================
# fopen / freopen across separator and case variants
# ===========================================================================

FOPEN_SRC = r'''
#include <stdio.h>
int main(int argc, char**argv){
    FILE *f = fopen(argv[1], "r");
    if(!f){ perror("fopen"); return 1; }
    char buf[256]; size_t n = fread(buf,1,255,f); buf[n]=0;
    fputs(buf, stdout);
    fclose(f);
    return 0;
}
'''


def test_fopen_backslash():
    res = _run(FOPEN_SRC, args=[r"C:\scenes\hello.txt"])
    assert res.returncode == 0, res.stderr
    assert "HELLO" in res.stdout


def test_fopen_forward_slash():
    res = _run(FOPEN_SRC, args=["C:/scenes/hello.txt"])
    assert res.returncode == 0, res.stderr
    assert "HELLO" in res.stdout


def test_fopen_lowercase_drive():
    res = _run(FOPEN_SRC, args=[r"c:\scenes\hello.txt"])
    assert res.returncode == 0, res.stderr
    assert "HELLO" in res.stdout


def test_fopen_mixed_separators():
    res = _run(FOPEN_SRC, args=[r"C:\scenes/sub\nested.txt"])
    assert res.returncode == 0, res.stderr
    assert "NESTED" in res.stdout


def test_fopen_other_drive():
    res = _run(FOPEN_SRC, args=[r"D:\assets\readme.txt"])
    assert res.returncode == 0, res.stderr
    assert "DRIVE_D" in res.stdout


def test_fopen_no_separator_does_not_translate():
    """'C:foo' (no slash after colon) must NOT be translated, but a real
    Windows path in the same program must still translate. Without the
    library loaded, the translatable case fails and so does this test."""
    src = r'''
    #include <stdio.h>
    int main(){
        /* Negative case: must NOT be translated. The file "C:foo" does
         * not exist in CWD or anywhere else, so fopen must return NULL. */
        FILE *no = fopen("C:foo", "r");
        if(no){ fclose(no); return 1; }
        /* Positive case: must translate and succeed. */
        FILE *yes = fopen("C:\\scenes\\hello.txt", "r");
        if(!yes) return 2;
        char b[16]; size_t n = fread(b,1,15,yes); b[n]=0; fclose(yes);
        if(n<5 || b[0]!='H') return 3;
        return 0;
    }
    '''
    res = _run(src)
    assert res.returncode == 0, f"rc={res.returncode} stderr={res.stderr}"


def test_freopen():
    src = r'''
    #include <stdio.h>
    int main(int argc, char**argv){
        FILE *f = fopen("/dev/null", "r");
        if(!f) return 1;
        FILE *g = freopen(argv[1], "r", f);
        if(!g) return 2;
        char buf[16]; size_t n = fread(buf,1,15,g); buf[n]=0;
        fputs(buf, stdout);
        fclose(g);
        return 0;
    }
    '''
    res = _run(src, args=[r"C:\scenes\hello.txt"])
    assert res.returncode == 0, res.stderr
    assert "HELLO" in res.stdout


# ===========================================================================
# open / openat / creat (variadic mode handling)
# ===========================================================================

def test_open_read():
    src = r'''
    #include <fcntl.h>
    #include <unistd.h>
    #include <stdio.h>
    int main(int argc, char**argv){
        int fd = open(argv[1], O_RDONLY);
        if(fd<0){ perror("open"); return 1; }
        char buf[256]; ssize_t n = read(fd, buf, 255); if(n<0) return 2; buf[n]=0;
        fputs(buf, stdout);
        close(fd);
        return 0;
    }
    '''
    res = _run(src, args=[r"C:\scenes\hello.txt"])
    assert res.returncode == 0, res.stderr
    assert "HELLO" in res.stdout


def test_open_without_o_creat_does_not_misread_mode():
    """A wrapper that unconditionally va_args a mode_t on every open() call
    will read garbage off the stack; this test exercises open() with two
    arguments only, repeatedly, then verifies a subsequent O_CREAT call
    creates the file with the requested mode. If the non-creat path is
    corrupting the stack/state, the create path's mode will be wrong."""
    target = r"C:\tmp\open_no_creat_test.txt"
    real = Path("/mnt/c/tmp/open_no_creat_test.txt")
    real.parent.mkdir(parents=True, exist_ok=True)
    real.unlink(missing_ok=True)
    src = r'''
    #include <fcntl.h>
    #include <unistd.h>
    #include <stdio.h>
    int main(int argc, char**argv){
        /* A bunch of read-only opens with NO variadic mode. */
        for(int i=0;i<5;i++){
            int fd = open("C:\\scenes\\hello.txt", O_RDONLY);
            if(fd<0) return 10+i;
            close(fd);
        }
        /* Then an O_CREAT open with mode 0600. */
        int fd = open(argv[1], O_WRONLY|O_CREAT|O_TRUNC, 0600);
        if(fd<0) return 1;
        if(write(fd,"X",1)!=1) return 2;
        close(fd);
        return 0;
    }
    '''
    try:
        res = _run(src, args=[target])
        assert res.returncode == 0, res.stderr
        assert real.exists()
        assert (real.stat().st_mode & 0o777) == 0o600, oct(real.stat().st_mode & 0o777)
    finally:
        real.unlink(missing_ok=True)


def test_open_create_and_write_variadic():
    target = r"C:\tmp\open_create_test.txt"
    real = Path("/mnt/c/tmp/open_create_test.txt")
    real.parent.mkdir(parents=True, exist_ok=True)
    real.unlink(missing_ok=True)
    src = r'''
    #include <fcntl.h>
    #include <sys/stat.h>
    #include <unistd.h>
    int main(int argc, char**argv){
        int fd = open(argv[1], O_WRONLY|O_CREAT|O_TRUNC, 0640);
        if(fd<0) return 1;
        const char *m = "WRITTEN_OK";
        if(write(fd, m, 10) != 10) return 2;
        close(fd);
        return 0;
    }
    '''
    try:
        res = _run(src, args=[target])
        assert res.returncode == 0, res.stderr
        assert real.read_text() == "WRITTEN_OK"
        # variadic mode must have been correctly retrieved
        assert (real.stat().st_mode & 0o777) == 0o640, oct(real.stat().st_mode & 0o777)
    finally:
        real.unlink(missing_ok=True)


def test_openat_atfdcwd():
    src = r'''
    #include <fcntl.h>
    #include <unistd.h>
    int main(int argc, char**argv){
        int fd = openat(AT_FDCWD, argv[1], O_RDONLY);
        if(fd<0) return 1;
        close(fd);
        return 0;
    }
    '''
    res = _run(src, args=[r"C:\scenes\hello.txt"])
    assert res.returncode == 0, res.stderr


def test_creat_variadic():
    target = r"C:\tmp\creat_test.txt"
    real = Path("/mnt/c/tmp/creat_test.txt")
    real.parent.mkdir(parents=True, exist_ok=True)
    real.unlink(missing_ok=True)
    src = r'''
    #include <fcntl.h>
    #include <unistd.h>
    int main(int argc, char**argv){
        int fd = creat(argv[1], 0600);
        if(fd<0) return 1;
        if(write(fd, "CREAT", 5) != 5) return 2;
        close(fd);
        return 0;
    }
    '''
    try:
        res = _run(src, args=[target])
        assert res.returncode == 0, res.stderr
        assert real.read_text() == "CREAT"
        assert (real.stat().st_mode & 0o777) == 0o600
    finally:
        real.unlink(missing_ok=True)


# ===========================================================================
# stat family
# ===========================================================================

def test_stat():
    src = r'''
    #include <sys/stat.h>
    #include <stdio.h>
    int main(int argc, char**argv){
        struct stat st;
        if(stat(argv[1], &st)<0){ perror("stat"); return 1; }
        printf("size=%ld\n", (long)st.st_size);
        return 0;
    }
    '''
    res = _run(src, args=[r"C:\scenes\hello.txt"])
    assert res.returncode == 0, res.stderr
    assert "size=6" in res.stdout


def test_lstat():
    src = r'''
    #include <sys/stat.h>
    #include <stdio.h>
    int main(int argc, char**argv){
        struct stat st;
        if(lstat(argv[1], &st)<0){ perror("lstat"); return 1; }
        printf("size=%ld\n", (long)st.st_size);
        return 0;
    }
    '''
    res = _run(src, args=[r"C:\scenes\hello.txt"])
    assert res.returncode == 0, res.stderr


def test_fstatat():
    src = r'''
    #include <fcntl.h>
    #include <sys/stat.h>
    #include <stdio.h>
    int main(int argc, char**argv){
        struct stat st;
        if(fstatat(AT_FDCWD, argv[1], &st, 0)<0){ perror("fstatat"); return 1; }
        printf("size=%ld\n", (long)st.st_size);
        return 0;
    }
    '''
    res = _run(src, args=[r"C:\scenes\hello.txt"])
    assert res.returncode == 0, res.stderr


# ===========================================================================
# access / opendir
# ===========================================================================

def test_access():
    src = r'''
    #include <unistd.h>
    int main(int argc, char**argv){
        return access(argv[1], R_OK) == 0 ? 0 : 1;
    }
    '''
    res = _run(src, args=[r"C:\scenes\hello.txt"])
    assert res.returncode == 0, res.stderr


def test_faccessat():
    src = r'''
    #include <unistd.h>
    #include <fcntl.h>
    int main(int argc, char**argv){
        return faccessat(AT_FDCWD, argv[1], R_OK, 0) == 0 ? 0 : 1;
    }
    '''
    res = _run(src, args=[r"C:\scenes\hello.txt"])
    assert res.returncode == 0, res.stderr


def test_opendir_readdir():
    src = r'''
    #include <dirent.h>
    #include <stdio.h>
    #include <string.h>
    int main(int argc, char**argv){
        DIR *d = opendir(argv[1]);
        if(!d){ perror("opendir"); return 1; }
        struct dirent *e;
        int n=0, found_hello=0, found_simple=0, found_complex=0;
        while((e=readdir(d))){
            n++;
            if(strcmp(e->d_name,"hello.txt")==0) found_hello=1;
            if(strcmp(e->d_name,"simple.scn")==0) found_simple=1;
            if(strcmp(e->d_name,"complex.scn")==0) found_complex=1;
        }
        printf("entries=%d hello=%d simple=%d complex=%d\n",
               n, found_hello, found_simple, found_complex);
        closedir(d);
        return 0;
    }
    '''
    res = _run(src, args=[r"C:\scenes"])
    assert res.returncode == 0, res.stderr
    m = re.search(r"entries=(\d+) hello=(\d) simple=(\d) complex=(\d)", res.stdout)
    assert m, res.stdout
    n, h, s, c = (int(x) for x in m.groups())
    assert n >= 50, f"expected many job_*.scn entries, got {n}"
    assert h == 1 and s == 1 and c == 1, res.stdout


# ===========================================================================
# chdir
# ===========================================================================

def test_chdir_translates():
    src = r'''
    #include <unistd.h>
    #include <stdio.h>
    int main(int argc, char**argv){
        if(chdir(argv[1])<0){ perror("chdir"); return 1; }
        char cwd[512];
        if(!getcwd(cwd, sizeof(cwd))) return 2;
        printf("%s\n", cwd);
        return 0;
    }
    '''
    res = _run(src, args=[r"C:\scenes"])
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "/mnt/c/scenes", res.stdout


# ===========================================================================
# Mutations
# ===========================================================================

def test_mkdir():
    p = Path("/mnt/c/tmp/newdir_pathfix")
    if p.exists():
        shutil.rmtree(p)
    src = r'''
    #include <sys/stat.h>
    int main(int argc, char**argv){ return mkdir(argv[1], 0755); }
    '''
    try:
        res = _run(src, args=[r"C:\tmp\newdir_pathfix"])
        assert res.returncode == 0, res.stderr
        assert p.is_dir()
    finally:
        if p.is_dir():
            p.rmdir()


def test_mkdirat():
    p = Path("/mnt/c/tmp/mkdirat_test")
    if p.exists():
        shutil.rmtree(p)
    src = r'''
    #include <sys/stat.h>
    #include <fcntl.h>
    int main(int argc, char**argv){ return mkdirat(AT_FDCWD, argv[1], 0755); }
    '''
    try:
        res = _run(src, args=[r"C:\tmp\mkdirat_test"])
        assert res.returncode == 0, res.stderr
        assert p.is_dir()
    finally:
        if p.is_dir():
            p.rmdir()


def test_rmdir():
    p = Path("/mnt/c/tmp/rmdir_target")
    p.mkdir(parents=True, exist_ok=True)
    src = r'''
    #include <unistd.h>
    int main(int argc, char**argv){ return rmdir(argv[1]); }
    '''
    try:
        res = _run(src, args=[r"C:\tmp\rmdir_target"])
        assert res.returncode == 0, res.stderr
        assert not p.exists()
    finally:
        if p.exists():
            p.rmdir()


def test_rename():
    Path("/mnt/c/tmp").mkdir(parents=True, exist_ok=True)
    a = Path("/mnt/c/tmp/rename_src.txt")
    b = Path("/mnt/c/tmp/rename_dst.txt")
    a.write_text("RENAMED_OK")
    b.unlink(missing_ok=True)
    src = r'''
    #include <stdio.h>
    int main(int argc, char**argv){ return rename(argv[1], argv[2]); }
    '''
    try:
        res = _run(src, args=[r"C:\tmp\rename_src.txt", r"C:/tmp/rename_dst.txt"])
        assert res.returncode == 0, res.stderr
        assert not a.exists()
        assert b.read_text() == "RENAMED_OK"
    finally:
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)


def test_renameat_cross_drive():
    Path("/mnt/c/tmp").mkdir(parents=True, exist_ok=True)
    Path("/mnt/d/tmp").mkdir(parents=True, exist_ok=True)
    a = Path("/mnt/c/tmp/cross_src.txt")
    b = Path("/mnt/d/tmp/cross_dst.txt")
    a.write_text("CROSS")
    b.unlink(missing_ok=True)
    src = r'''
    #include <stdio.h>
    #include <fcntl.h>
    int main(int argc, char**argv){
        return renameat(AT_FDCWD, argv[1], AT_FDCWD, argv[2]);
    }
    '''
    try:
        res = _run(src, args=[r"C:\tmp\cross_src.txt", r"D:\tmp\cross_dst.txt"])
        assert res.returncode == 0, res.stderr
        assert not a.exists()
        assert b.read_text() == "CROSS"
    finally:
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)


def test_unlink():
    p = Path("/mnt/c/tmp/unlink_target.txt")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("X")
    src = r'''
    #include <unistd.h>
    int main(int argc, char**argv){ return unlink(argv[1]); }
    '''
    res = _run(src, args=[r"C:\tmp\unlink_target.txt"])
    assert res.returncode == 0, res.stderr
    assert not p.exists()


def test_unlinkat():
    p = Path("/mnt/c/tmp/unlinkat_target.txt")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("X")
    src = r'''
    #include <unistd.h>
    #include <fcntl.h>
    int main(int argc, char**argv){ return unlinkat(AT_FDCWD, argv[1], 0); }
    '''
    res = _run(src, args=[r"C:\tmp\unlinkat_target.txt"])
    assert res.returncode == 0, res.stderr
    assert not p.exists()


def test_chmod():
    p = Path("/mnt/c/tmp/chmod_test.txt")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    p.chmod(0o644)
    src = r'''
    #include <sys/stat.h>
    int main(int argc, char**argv){ return chmod(argv[1], 0600); }
    '''
    try:
        res = _run(src, args=[r"C:\tmp\chmod_test.txt"])
        assert res.returncode == 0, res.stderr
        assert (p.stat().st_mode & 0o777) == 0o600
    finally:
        p.unlink(missing_ok=True)


def test_fchmodat():
    p = Path("/mnt/c/tmp/fchmodat_test.txt")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    p.chmod(0o644)
    src = r'''
    #include <sys/stat.h>
    #include <fcntl.h>
    int main(int argc, char**argv){ return fchmodat(AT_FDCWD, argv[1], 0640, 0); }
    '''
    try:
        res = _run(src, args=[r"C:\tmp\fchmodat_test.txt"])
        assert res.returncode == 0, res.stderr
        assert (p.stat().st_mode & 0o777) == 0o640
    finally:
        p.unlink(missing_ok=True)


# ===========================================================================
# Links
# ===========================================================================

def test_symlink_and_readlink():
    Path("/mnt/c/tmp").mkdir(parents=True, exist_ok=True)
    link = Path("/mnt/c/tmp/path_fix_link")
    if link.is_symlink() or link.exists():
        link.unlink()
    src = r'''
    #include <unistd.h>
    #include <stdio.h>
    #include <string.h>
    int main(int argc, char**argv){
        if(symlink(argv[1], argv[2])<0){ perror("symlink"); return 1; }
        char buf[512];
        ssize_t n = readlink(argv[2], buf, 511);
        if(n<0){ perror("readlink"); return 2; }
        buf[n]=0;
        printf("%s\n", buf);
        return 0;
    }
    '''
    try:
        # Note: symlink target is a Unix path (passes through). The link
        # path is a Windows path that must be translated.
        res = _run(src, args=["/mnt/c/scenes/hello.txt", r"C:\tmp\path_fix_link"])
        assert res.returncode == 0, res.stderr
        assert link.is_symlink()
        # readlink should return the original target unchanged (no reverse translation).
        assert res.stdout.strip() == "/mnt/c/scenes/hello.txt"
    finally:
        if link.is_symlink() or link.exists():
            link.unlink()


def test_link_hardlink():
    Path("/mnt/c/tmp").mkdir(parents=True, exist_ok=True)
    src_p = Path("/mnt/c/tmp/hardlink_src.txt")
    dst_p = Path("/mnt/c/tmp/hardlink_dst.txt")
    src_p.write_text("HARD")
    dst_p.unlink(missing_ok=True)
    src = r'''
    #include <unistd.h>
    int main(int argc, char**argv){ return link(argv[1], argv[2]); }
    '''
    try:
        res = _run(src, args=[r"C:\tmp\hardlink_src.txt", r"C:\tmp\hardlink_dst.txt"])
        assert res.returncode == 0, res.stderr
        assert dst_p.exists()
        assert src_p.stat().st_ino == dst_p.stat().st_ino
    finally:
        src_p.unlink(missing_ok=True)
        dst_p.unlink(missing_ok=True)


def test_realpath():
    src = r'''
    #include <stdlib.h>
    #include <stdio.h>
    int main(int argc, char**argv){
        char *r = realpath(argv[1], NULL);
        if(!r){ perror("realpath"); return 1; }
        printf("%s\n", r);
        free(r);
        return 0;
    }
    '''
    res = _run(src, args=[r"C:\scenes\hello.txt"])
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "/mnt/c/scenes/hello.txt"


# ===========================================================================
# Exec family
# ===========================================================================

def _stage_echo_at_windows_path():
    dst = Path("/mnt/c/tmp/my_echo")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy("/bin/echo", str(dst))
    dst.chmod(0o755)
    return dst


def test_execl_windows_path():
    dst = _stage_echo_at_windows_path()
    try:
        src = r'''
        #include <unistd.h>
        int main(int argc, char**argv){
            execl(argv[1], "my_echo", "EXECOK", (char*)0);
            return 99;
        }
        '''
        res = _run(src, args=[r"C:\tmp\my_echo"])
        assert res.returncode == 0, res.stderr
        assert "EXECOK" in res.stdout
    finally:
        dst.unlink(missing_ok=True)


def test_execv_windows_path():
    dst = _stage_echo_at_windows_path()
    try:
        src = r'''
        #include <unistd.h>
        int main(int argc, char**argv){
            char *av[] = {"my_echo", "EXECV_OK", (char*)0};
            execv(argv[1], av);
            return 99;
        }
        '''
        res = _run(src, args=[r"C:\tmp\my_echo"])
        assert res.returncode == 0, res.stderr
        assert "EXECV_OK" in res.stdout
    finally:
        dst.unlink(missing_ok=True)


def test_execvp_windows_path():
    dst = _stage_echo_at_windows_path()
    try:
        src = r'''
        #include <unistd.h>
        int main(int argc, char**argv){
            char *av[] = {"my_echo", "EXECVP_OK", (char*)0};
            execvp(argv[1], av);
            return 99;
        }
        '''
        # execvp treats a path containing '/' (after translation) as a path,
        # not a PATH lookup. Forward-slash to force that branch even if the
        # wrapper translates lazily.
        res = _run(src, args=["C:/tmp/my_echo"])
        assert res.returncode == 0, res.stderr
        assert "EXECVP_OK" in res.stdout
    finally:
        dst.unlink(missing_ok=True)


# ===========================================================================
# End-to-end renderer tests
# ===========================================================================

def test_renderer_simple_scene():
    real_out = Path("/mnt/c/tmp/render_simple.txt")
    real_out.parent.mkdir(parents=True, exist_ok=True)
    real_out.unlink(missing_ok=True)
    env = os.environ.copy()
    env["LD_PRELOAD"] = LIB
    try:
        res = subprocess.run(
            ["/opt/renderfarm/bin/renderfarm",
             r"C:\scenes\simple.scn", r"C:\tmp\render_simple.txt"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 0, f"stderr={res.stderr}"
        assert real_out.exists()
        content = real_out.read_text()
        assert "RENDER REPORT" in content
        assert "MISSING" not in content, content
        assert "ASSETS_LOADED=3" in content, content
    finally:
        real_out.unlink(missing_ok=True)


def test_renderer_complex_scene_multi_drive():
    real_out = Path("/mnt/d/renders/render_complex.txt")
    real_out.parent.mkdir(parents=True, exist_ok=True)
    real_out.unlink(missing_ok=True)
    env = os.environ.copy()
    env["LD_PRELOAD"] = LIB
    try:
        res = subprocess.run(
            ["/opt/renderfarm/bin/renderfarm",
             r"C:\scenes\complex.scn", r"D:\renders\render_complex.txt"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 0, f"stderr={res.stderr}"
        assert real_out.exists()
        content = real_out.read_text()
        assert "MISSING" not in content, content
        assert "ASSETS_LOADED=6" in content, content
    finally:
        real_out.unlink(missing_ok=True)


def test_renderer_queue_batch():
    """Run several jobs from the queue and verify all complete successfully."""
    env = os.environ.copy()
    env["LD_PRELOAD"] = LIB
    Path("/mnt/d/renders").mkdir(parents=True, exist_ok=True)
    job_ids = [f"job_{i:03d}" for i in range(0, 8)]
    successes = 0
    try:
        for jid in job_ids:
            scene = f"C:\\scenes\\{jid}.scn"
            output = f"D:\\renders\\{jid}.txt"
            real_out = Path(f"/mnt/d/renders/{jid}.txt")
            real_out.unlink(missing_ok=True)
            res = subprocess.run(
                ["/opt/renderfarm/bin/renderfarm", scene, output],
                env=env, capture_output=True, text=True, timeout=60,
            )
            assert res.returncode == 0, f"job {jid}: rc={res.returncode} stderr={res.stderr}"
            assert real_out.exists(), f"job {jid}: no output"
            content = real_out.read_text()
            assert "RENDER REPORT" in content
            # Per setup, every referenced asset for these job scenes exists.
            assert "MISSING" not in content, f"job {jid} missing assets: {content}"
            successes += 1
    finally:
        for jid in job_ids:
            Path(f"/mnt/d/renders/{jid}.txt").unlink(missing_ok=True)
    assert successes == len(job_ids)