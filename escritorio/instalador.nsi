Unicode true
SetCompressor /SOLID lzma
!include "MUI2.nsh"
Name "Lumbre"
OutFile "${SALIDA}"
InstallDir "$LOCALAPPDATA\Programs\Lumbre"
InstallDirRegKey HKCU "Software\Lumbre" "Carpeta"
RequestExecutionLevel user
BrandingText "Lumbre ${VERSION}"
!define MUI_ICON "lumbre.ico"
!define MUI_UNICON "lumbre.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\Lumbre.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Abrir Lumbre"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Spanish"

Section
  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\codigo"
  RMDir /r "$INSTDIR\runtime"
  File /r "${ORIGEN}\*.*"
  WriteUninstaller "$INSTDIR\Desinstalar.exe"
  CreateShortcut "$SMPROGRAMS\Lumbre.lnk" "$INSTDIR\Lumbre.exe"
  CreateShortcut "$DESKTOP\Lumbre.lnk" "$INSTDIR\Lumbre.exe"
  WriteRegStr HKCU "Software\Lumbre" "Carpeta" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lumbre" "DisplayName" "Lumbre"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lumbre" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lumbre" "DisplayIcon" "$INSTDIR\Lumbre.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lumbre" "Publisher" "Lumbre"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lumbre" "UninstallString" '"$INSTDIR\Desinstalar.exe"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lumbre" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lumbre" "NoRepair" 1
  ; Una actualización anterior no debe sustituir a una instalación más reciente.
  Delete "$LOCALAPPDATA\Lumbre\actualizaciones\actual.json"
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\Lumbre.lnk"
  Delete "$DESKTOP\Lumbre.lnk"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lumbre"
  DeleteRegKey HKCU "Software\Lumbre"
SectionEnd
