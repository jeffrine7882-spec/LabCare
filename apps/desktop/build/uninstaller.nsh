; LabSynch Alerts — uninstaller cleanup (hooked in via nsis.include).
;
; The app writes two "ring even when closed" mechanisms at RUNTIME, which
; electron-builder's uninstaller does not know about:
;   * the per-minute watchdog Scheduled Task (main.js ensureWatchdogTask)
;   * the auto-start Run-key entry (app.setLoginItemSettings)
; Without this hook, uninstalling would leave a ghost task firing every
; minute at a deleted exe and a dead Run-key entry.

!macro customUnInstall
  ; /F: delete even if a watchdog relaunch is mid-flight
  ExecWait '"$SYSDIR\schtasks.exe" /Delete /TN "LabSynch Alerts Watchdog" /F'
  ; setLoginItemSettings names the value after the product; delete both
  ; spellings so no dead entry survives either way.
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "LabSynch Alerts"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "com.insforge.labcare"
!macroend
