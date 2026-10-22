# Проверка Docker после исправления H03

Статус 10C, 20–21.10.2026: **фактический Docker runtime проверен**.
Docker Desktop 4.86.0, Engine/Client 29.7.2, Linux/WSL2. Пройдены сборка,
UID/GID 1000:1000, общий lock, SQLite, HTTP, оба DOCX и сохранность после restart.
Проверены новые named volumes, новый Windows bind mount, отдельный Compose-проект
и восстановленная синтетическая копия с данными в прежнем корне volume.
Подробности, команды и пределы: [RELEASE_CANDIDATE_REPORT.md](RELEASE_CANDIDATE_REPORT.md).

Первое обращение из ограниченной песочницы получило отказ доступа к Docker config
и named pipe. Повтор вне песочницы подтвердил работающий Engine; это не ошибка
исправленной конфигурации. Существующие контейнеры и данные других проектов
не изменялись. Runtime приложения и все probes выполнялись non-root.

## Каталоги и права

Образ создаёт пользователя app с UID/GID `1000:1000` и передаёт ему
`/var/lib/daa` и `/var/lib/daa/data`. Backend работает с
`DAA_DATA_DIR=/var/lib/daa/data`. `DataLease` по-прежнему создаёт lock-файл
в родительском каталоге данных; код блокировки не изменён.

В Compose два локальных named volume:

| Volume | Путь в контейнере | Назначение |
|---|---|---|
| workflow-data | /var/lib/daa/data | Прежняя БД и файлы в прежнем корне volume |
| workflow-state | /var/lib/daa | Общий родитель и lock-файл |

Имя `workflow-data` сохранено. Изменяется только точка монтирования внутри
контейнера; данные не перемещаются и SQLite не мигрирует. При обновлении сохранить
тот же Compose project name: изменение имени проекта выбирает другие volumes.
Остановить все старые backend перед переходом. Не запускать версии со старым
и новым расположением lock одновременно.

Новый пустой named volume получает содержимое каталога образа по правилам
[Docker volumes](https://docs.docker.com/engine/storage/volumes/).
**Существующий volume не получает новые права из Dockerfile.** Его владельцы
должны разрешать UID/GID 1000 запись в данные и общий родитель. Проверить владельцев
до запуска; при несовпадении сначала сохранить копию и согласовать изменение
прав на конкретный volume. Автоматического chown пользовательских данных нет.
`root`, `chmod 777` и отдельный приватный lock не являются решением.

Bind mount скрывает права и содержимое каталога образа:
[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/).
На Linux владелец заранее создаёт отдельный родитель с правами UID/GID 1000.
На Docker Desktop доступ к Windows-каталогу зависит также от настроек файлового
доступа и ACL. Монтировать **родителя целиком** в `/var/lib/daa`, внутри него
приложение создаст `data`. Не монтировать существующий каталог данных как
родителя: это другой layout. Для прежнего layout использовать оба named volume
из Compose либо явно подготовленную пару родитель/data mounts.

Все процессы обслуживания должны видеть те же volumes в тех же контейнерных
путях. Нельзя обслуживать эти данные одновременно через произвольный путь хоста:
ключ DataLease зависит от абсолютного пути. Эта конфигурация не поддерживает
несколько backend workers.

## Воспроизводимый smoke-тест

Из корня checkout в PowerShell, после запуска Docker Desktop в режиме Linux
containers и установки Python-зависимостей проекта:

```powershell
docker version
docker compose config --quiet
python -B scripts/smoke_docker.py
```

Скрипт создаёт уникальные имена образа, контейнера и **новых** synthetic volumes.
Он не подключает рабочие volumes:

1. `docker build` собирает текущий Dockerfile.
2. Backend стартует non-root с двумя mounts как в Compose, портом только localhost
   и явным offline-режимом.
3. Проверяются `/health`, реальные UID/GID, доступ к записи и создание lock-файла.
4. Синтетический комплект `demo/A` проходит POST `/runs`, генерацию и скачивание
   обоих DOCX. Число LLM-попыток должно быть нулевым.
5. Второй non-root контейнер с теми же mounts пытается получить maintenance lease
   и обязан получить отказ, пока backend работает.
6. После restart тот же Idempotency-Key возвращает прежний run_id; usage и
   байты обоих DOCX должны совпасть.
7. Backend останавливается, maintenance lease становится доступен.

Успех — финальный JSON с `"passed": true`. Ошибка любого шага завершает команду
ненулевым кодом. Скрипт останавливает созданный backend, но оставляет контейнер,
образ и volumes для разбора; их уникальные имена напечатаны в начале. Не применять
`docker system prune` или удаление рабочих volumes для очистки smoke-теста.

Проверка bind mount выполняется отдельно, на **новом пустом** каталоге:

```powershell
New-Item -ItemType Directory -Path C:\daa-smoke-bind
python -B scripts/smoke_docker.py --bind-parent C:\daa-smoke-bind
```

Команда `New-Item` должна создать новый каталог; не использовать существующий
рабочий каталог. На Linux пример подготовки отдельного тестового каталога
администратором: `sudo install -d -o 1000 -g 1000 -m 0700 /srv/daa-smoke-bind`,
затем `python -B scripts/smoke_docker.py --bind-parent /srv/daa-smoke-bind`.
Проверка bind не заменяет проверку named volumes, и наоборот.

Для Compose после smoke нужны `docker compose build api`, `docker compose up -d api`
и HTTP `/health` на настроенном localhost-порту. Проверку старого volume выполнять
на восстановленной синтетической копии с прежним layout, а не на рабочих данных.
Сборка и download зависимостей требуют сети; LLM-вызовов smoke не выполняет.

## Обслуживание

Остановить API: `docker compose stop api`. Затем одноразовый контейнер через
`docker compose run --rm --no-deps api ...` наследует оба shared mounts.
Путь данных для CLI backup/verify/restore — `/var/lib/daa/data`.
Каталог backup подключать отдельно с подходящими правами, за пределами данных,
и защищать от постороннего доступа. Общие правила:
[BACKUP_AND_RECOVERY.md](BACKUP_AND_RECOVERY.md).

Локальные тесты в `tests/test_docker_configuration.py` проверяют layout,
сохранение имени volume, bind-конфигурацию, взаимное исключение двух процессов
и безопасный отказ при ошибке прав. Они выполняются на Windows/NTFS и не
доказывают Linux runtime, copy-up прав Docker, nested mounts или работу bind
в Docker Desktop. Runtime-проверки дополнительно выполнены в 10C; конкретные результаты
приведены в отчёте кандидата. Это не универсальная гарантия прав любого существующего
volume, Windows ACL или другой Linux-файловой системы.
