/* Abre Lumbre sin ventana de consola. */
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE i, HINSTANCE p, PWSTR args, int show) {
    (void)i; (void)p; (void)args; (void)show;
    wchar_t dir[32768];
    DWORD n = GetModuleFileNameW(NULL, dir, 32768);
    if (!n || n >= 32768) return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (!slash) return 1;
    *slash = 0;

    static wchar_t path[65536], old[32768], python[33000], script[33000], cmd[67000];
    GetEnvironmentVariableW(L"PATH", old, 32768);
    _snwprintf(path, 65536, L"%ls\\runtime;%ls\\runtime\\Library\\bin;%ls\\runtime\\Scripts;%ls", dir, dir, dir, old);
    SetEnvironmentVariableW(L"PATH", path);
    SetEnvironmentVariableW(L"PYTHONHOME", NULL);
    SetEnvironmentVariableW(L"PYTHONPATH", NULL);
    SetEnvironmentVariableW(L"PYTHONNOUSERSITE", L"1");
    SetEnvironmentVariableW(L"PYTHONUTF8", L"1");
    SetEnvironmentVariableW(L"PYTHONDONTWRITEBYTECODE", L"1");

    _snwprintf(python, 33000, L"%ls\\runtime\\pythonw.exe", dir);
    _snwprintf(script, 33000, L"%ls\\arranque.py", dir);
    _snwprintf(cmd, 67000, L"\"%ls\" \"%ls\"", python, script);

    STARTUPINFOW si; PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si); si.cb = sizeof si; ZeroMemory(&pi, sizeof pi);
    if (!CreateProcessW(python, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, dir, &si, &pi)) {
        MessageBoxW(NULL, L"No se ha podido abrir Lumbre. Prueba a reinstalarla.", L"Lumbre", MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    return 0;
}
