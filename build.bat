@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo [1/3] 安装依赖...
python -m pip install --upgrade pillow pyinstaller || goto :fail

echo [2/3] 打包单文件 exe（无控制台、带图标）...
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --icon "assets\sharexflash.ico" ^
    --add-data "assets\sharexflash.ico;." ^
    --name "ShareX Flash" ^
    --distpath "dist" ^
    --workpath "build" ^
    --specpath "." ^
    "src\sharexflash.py" || goto :fail

echo [3/3] 完成。
echo.
echo 构建完成：%~dp0dist\ShareX Flash.exe
goto :eof

:fail
echo.
echo 构建失败，请检查上方错误信息。
exit /b 1
