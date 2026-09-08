/* xinput_gate.c — fake XInput DLL for 凝冰结魄 handpad isolation
 *
 * 编译（需 MSVC Build Tools + Windows SDK）：
 *   cl /nologo /LD /O2 /DWANT_XINPUT13  /Fe:xinput1_3.dll     xinput_gate.c /link /DEF:xinput1_3.def
 *   cl /nologo /LD /O2 /DWANT_XINPUT14  /Fe:xinput1_4.dll     xinput_gate.c /link /DEF:xinput1_4.def
 *   cl /nologo /LD /O2 /DWANT_XINPUT910 /Fe:XInput9_1_0.dll   xinput_gate.c /link /DEF:XInput9_1_0.def
 *
 * 工作原理：
 *   1. DllMain 加载真实系统 XInput DLL（绝对路径，避免递归加载自己）
 *   2. DllMain 打开凝冰结魄命名事件 Local\NingBingJueGamepadGate
 *   3. XInputGetState / GetStateEx / GetCapabilities / SetState 每次调用检查事件信号态：
 *      - 信号态（凝冰结魄前台）→ 返回 ERROR_DEVICE_NOT_CONNECTED
 *      - 非信号态（凝冰结魄失焦/未启动）→ 透传真实状态
 *   4. 事件打开失败（凝冰结魄没启动）→ gate_is_signaled 返回 FALSE → 透传
 *
 *  fallback：真实系统 DLL 加载失败时，所有函数返回 ERROR_DEVICE_NOT_CONNECTED
 *            （游戏会认为手柄断开，不会崩溃）
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

/* ---- XInput 结构体定义（与 xinput.h 一致，避免依赖 SDK 头文件） ---- */

typedef struct _XINPUT_GAMEPAD {
    WORD  wButtons;
    BYTE  bLeftTrigger;
    BYTE  bRightTrigger;
    SHORT sThumbLX;
    SHORT sThumbLY;
    SHORT sThumbRX;
    SHORT sThumbRY;
} XINPUT_GAMEPAD;

typedef struct _XINPUT_STATE {
    DWORD          dwPacketNumber;
    XINPUT_GAMEPAD Gamepad;
} XINPUT_STATE;

typedef struct _XINPUT_VIBRATION {
    WORD wLeftMotorSpeed;
    WORD wRightMotorSpeed;
} XINPUT_VIBRATION;

typedef struct _XINPUT_CAPABILITIES {
    BYTE            Type;
    BYTE            SubType;
    WORD            Flags;
    XINPUT_GAMEPAD  Gamepad;
    XINPUT_VIBRATION Vibration;
} XINPUT_CAPABILITIES;

typedef struct _XINPUT_BATTERY_INFORMATION {
    BYTE BatteryType;
    BYTE BatteryLevel;
} XINPUT_BATTERY_INFORMATION;

/* ---- 常量 ---- */

#define NBG_ERROR_SUCCESS              0L
#define NBG_ERROR_DEVICE_NOT_CONNECTED 1167L  /* 0x048F */

/* 命名事件名（与凝冰结魄 xinput_gate.py 的 GATE_EVENT_NAME 一致） */
#define NBG_GATE_EVENT_NAME L"Local\\NingBingJueGamepadGate"

/* ---- 真实系统 DLL 路径（绝对路径，避免递归加载自己） ---- */

#if defined(WANT_XINPUT13)
  #define NBG_REAL_DLL_PATH L"C:\\Windows\\System32\\xinput1_3.dll"
#elif defined(WANT_XINPUT14)
  #define NBG_REAL_DLL_PATH L"C:\\Windows\\System32\\xinput1_4.dll"
#elif defined(WANT_XINPUT910)
  #define NBG_REAL_DLL_PATH L"C:\\Windows\\System32\\XInput9_1_0.dll"
#else
  #error "Must define one of WANT_XINPUT13 / WANT_XINPUT14 / WANT_XINPUT910"
#endif

/* ---- 函数指针类型 ---- */

typedef DWORD (WINAPI *PFN_XInputGetState)(DWORD, XINPUT_STATE*);
typedef DWORD (WINAPI *PFN_XInputGetStateEx)(DWORD, XINPUT_STATE*);
typedef DWORD (WINAPI *PFN_XInputSetState)(DWORD, XINPUT_VIBRATION*);
typedef DWORD (WINAPI *PFN_XInputGetCapabilities)(DWORD, DWORD, XINPUT_CAPABILITIES*);
typedef void  (WINAPI *PFN_XInputEnable)(BOOL);
typedef DWORD (WINAPI *PFN_XInputGetAudioDeviceIds)(DWORD, LPWSTR, UINT*, LPWSTR, UINT*);
typedef DWORD (WINAPI *PFN_XInputGetDSoundAudioDeviceGuids)(DWORD, GUID*, GUID*);
typedef DWORD (WINAPI *PFN_XInputGetBatteryInformation)(DWORD, BYTE, XINPUT_BATTERY_INFORMATION*);

/* ---- 全局状态 ---- */

static HMODULE g_real_dll = NULL;
static HANDLE  g_gate_event = NULL;  /* 命名事件，NULL = 凝冰结魄未启动 → 透传 */

static PFN_XInputGetState                   g_real_GetState = NULL;
static PFN_XInputGetStateEx                 g_real_GetStateEx = NULL;
static PFN_XInputSetState                   g_real_SetState = NULL;
static PFN_XInputGetCapabilities            g_real_GetCapabilities = NULL;
static PFN_XInputEnable                     g_real_Enable = NULL;
static PFN_XInputGetAudioDeviceIds          g_real_GetAudioDeviceIds = NULL;
static PFN_XInputGetDSoundAudioDeviceGuids  g_real_GetDSoundAudioDeviceGuids = NULL;
static PFN_XInputGetBatteryInformation      g_real_GetBatteryInformation = NULL;

/* ---- gate 信号检查 ---- */

/* 返回 TRUE = 凝冰结魄前台（拦截，让游戏看不到手柄）
 * 返回 FALSE = 凝冰结魄失焦/未启动（透传真实状态）
 *
 * WaitForSingleObject(gate, 0):
 *   WAIT_OBJECT_0 (0)  = 信号态（凝冰结魄前台）→ 拦截
 *   WAIT_TIMEOUT (258) = 非信号态（凝冰结魄失焦）→ 透传
 *   失败（g_gate_event == NULL）→ 透传
 */
static BOOL gate_is_signaled(void) {
    if (g_gate_event == NULL) return FALSE;
    return WaitForSingleObject(g_gate_event, 0) == WAIT_OBJECT_0;
}

/* ---- DllMain ---- */

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved) {
    (void)hinstDLL; (void)lpvReserved;

    if (fdwReason == DLL_PROCESS_ATTACH) {
        /* 1. 加载真实系统 XInput DLL（绝对路径，避免递归加载自己） */
        g_real_dll = LoadLibraryW(NBG_REAL_DLL_PATH);
        if (g_real_dll != NULL) {
            g_real_GetState                  = (PFN_XInputGetState)GetProcAddress(g_real_dll, "XInputGetState");
            g_real_GetStateEx                = (PFN_XInputGetStateEx)GetProcAddress(g_real_dll, (LPCSTR)100);
            g_real_SetState                  = (PFN_XInputSetState)GetProcAddress(g_real_dll, "XInputSetState");
            g_real_GetCapabilities           = (PFN_XInputGetCapabilities)GetProcAddress(g_real_dll, "XInputGetCapabilities");
            g_real_Enable                    = (PFN_XInputEnable)GetProcAddress(g_real_dll, "XInputEnable");
            g_real_GetAudioDeviceIds         = (PFN_XInputGetAudioDeviceIds)GetProcAddress(g_real_dll, "XInputGetAudioDeviceIds");
            g_real_GetDSoundAudioDeviceGuids = (PFN_XInputGetDSoundAudioDeviceGuids)GetProcAddress(g_real_dll, "XInputGetDSoundAudioDeviceGuids");
            g_real_GetBatteryInformation     = (PFN_XInputGetBatteryInformation)GetProcAddress(g_real_dll, "XInputGetBatteryInformation");
        }
        /* 2. 打开凝冰结魄命名事件（失败=NULL，gate_is_signaled 会返回 FALSE → 透传） */
        g_gate_event = OpenEventW(SYNCHRONIZE, FALSE, NBG_GATE_EVENT_NAME);
        /* 注意：不缓存事件句柄的"打开失败"状态——凝冰结魄可能在我们之后启动，
         *      所以每次 gate_is_signaled 调用时如果 g_gate_event==NULL 会尝试重开。
         *      但 OpenEventW 每次调用有开销，这里在 DllMain 打开一次，
         *      若凝冰结魄后启动则 g_gate_event 一直为 NULL（透传）直到游戏重启。
         *      这是可接受的妥协——用户通常先启动凝冰结魄再启动游戏。 */
    } else if (fdwReason == DLL_PROCESS_DETACH) {
        if (g_gate_event != NULL) {
            CloseHandle(g_gate_event);
            g_gate_event = NULL;
        }
        if (g_real_dll != NULL) {
            FreeLibrary(g_real_dll);
            g_real_dll = NULL;
        }
    }
    return TRUE;
}

/* ---- 导出函数实现 ---- */

/* XInputGetState — 核心 hook 目标
 * 凝冰结魄前台时返回 ERROR_DEVICE_NOT_CONNECTED（游戏看到手柄断开）
 * 失焦/未启动时透传真实状态 */
DWORD WINAPI XInputGetState(DWORD dwUserIndex, XINPUT_STATE *pState) {
    if (gate_is_signaled()) {
        if (pState) ZeroMemory(pState, sizeof(*pState));
        return NBG_ERROR_DEVICE_NOT_CONNECTED;
    }
    if (g_real_GetState) return g_real_GetState(dwUserIndex, pState);
    if (pState) ZeroMemory(pState, sizeof(*pState));
    return NBG_ERROR_DEVICE_NOT_CONNECTED;
}

/* XInputGetStateEx (ordinal 100) — 同 XInputGetState 逻辑
 * 很多游戏用这个（能读到 GUIDE 按钮） */
DWORD WINAPI XInputGetStateEx(DWORD dwUserIndex, XINPUT_STATE *pState) {
    if (gate_is_signaled()) {
        if (pState) ZeroMemory(pState, sizeof(*pState));
        return NBG_ERROR_DEVICE_NOT_CONNECTED;
    }
    if (g_real_GetStateEx) return g_real_GetStateEx(dwUserIndex, pState);
    /* 真实 DLL 没有 GetStateEx（罕见），退化到 GetState */
    if (g_real_GetState) return g_real_GetState(dwUserIndex, pState);
    if (pState) ZeroMemory(pState, sizeof(*pState));
    return NBG_ERROR_DEVICE_NOT_CONNECTED;
}

/* XInputSetState — 震动控制，凝冰结魄前台时吞掉 */
DWORD WINAPI XInputSetState(DWORD dwUserIndex, XINPUT_VIBRATION *pVibration) {
    if (gate_is_signaled()) {
        return NBG_ERROR_DEVICE_NOT_CONNECTED;
    }
    if (g_real_SetState) return g_real_SetState(dwUserIndex, pVibration);
    return NBG_ERROR_DEVICE_NOT_CONNECTED;
}

/* XInputGetCapabilities — 凝冰结魄前台时返回断开 */
DWORD WINAPI XInputGetCapabilities(DWORD dwUserIndex, DWORD dwFlags, XINPUT_CAPABILITIES *pCapabilities) {
    if (gate_is_signaled()) {
        if (pCapabilities) ZeroMemory(pCapabilities, sizeof(*pCapabilities));
        return NBG_ERROR_DEVICE_NOT_CONNECTED;
    }
    if (g_real_GetCapabilities) return g_real_GetCapabilities(dwUserIndex, dwFlags, pCapabilities);
    if (pCapabilities) ZeroMemory(pCapabilities, sizeof(*pCapabilities));
    return NBG_ERROR_DEVICE_NOT_CONNECTED;
}

/* XInputEnable — 进程内 API，不影响其他进程，直接透传 */
void WINAPI XInputEnable(BOOL enable) {
    if (g_real_Enable) g_real_Enable(enable);
}

/* XInputGetAudioDeviceIds — xinput1_4 only，透传 */
DWORD WINAPI XInputGetAudioDeviceIds(DWORD dwUserIndex, LPWSTR pRenderDeviceId, UINT *pRenderCount, LPWSTR pCaptureDeviceId, UINT *pCaptureCount) {
    if (g_real_GetAudioDeviceIds) return g_real_GetAudioDeviceIds(dwUserIndex, pRenderDeviceId, pRenderCount, pCaptureDeviceId, pCaptureCount);
    return NBG_ERROR_DEVICE_NOT_CONNECTED;
}

/* XInputGetDSoundAudioDeviceGuids — xinput1_3 only，透传 */
DWORD WINAPI XInputGetDSoundAudioDeviceGuids(DWORD dwUserIndex, GUID *pDSoundRenderGuid, GUID *pDSoundCaptureGuid) {
    if (g_real_GetDSoundAudioDeviceGuids) return g_real_GetDSoundAudioDeviceGuids(dwUserIndex, pDSoundRenderGuid, pDSoundCaptureGuid);
    return NBG_ERROR_DEVICE_NOT_CONNECTED;
}

/* XInputGetBatteryInformation — xinput1_4 only，透传 */
DWORD WINAPI XInputGetBatteryInformation(DWORD dwUserIndex, BYTE devType, XINPUT_BATTERY_INFORMATION *pBatteryInformation) {
    if (g_real_GetBatteryInformation) return g_real_GetBatteryInformation(dwUserIndex, devType, pBatteryInformation);
    if (pBatteryInformation) ZeroMemory(pBatteryInformation, sizeof(*pBatteryInformation));
    return NBG_ERROR_DEVICE_NOT_CONNECTED;
}
