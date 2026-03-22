PocketPaw — патч интеграции LM Studio (все правки в одном файле)

Файл: pocketpaw-lmstudio-full.patch

Как применить после git pull / обновления исходников:

1) Откройте терминал в корне репозитория pocketpaw (рядом с папкой lmstudio-integration).

2) Выполните одно из:
   git apply --whitespace=fix lmstudio-integration/pocketpaw-lmstudio-full.patch
   или дважды щёлкните: lmstudio-integration\apply-patch.bat

3) Если конфликты:
   git apply --reject --whitespace=fix lmstudio-integration/pocketpaw-lmstudio-full.patch
   рядом с файлами появятся *.rej — разберите вручную.

4) После применения при необходимости:
   uv tool install --force --editable .
   или установите зависимости проекта по README upstream.

5) Настройка конфига под LM Studio (опционально):
   lmstudio-integration\run-lmstudio-integration.bat
   или: py -3 lmstudio-integration\apply_lmstudio_config.py --model YOUR_MODEL_ID

Патч сформирован как diff к origin/main и включает новые файлы (lmstudio.py, скрипты в lmstudio-integration) и изменения во всех затронутых путях репозитория.
