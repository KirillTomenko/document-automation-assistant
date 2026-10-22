# PUBLICATION_DIFF_REVIEW — Document Automation Assistant 2.0.0

> **Итоговое обновление 22.10.2026:** 10F закрыл dependency-блокер. Зафиксированы Streamlit 1.66.1, Pillow 13.0.0 и pypdf 6.20.1; pip check успешен, pip-audit сообщил 0 известных уязвимостей проверенных зависимостей, регрессия — 699 passed, Ruff и Docker smoke прошли. Два Streamlit-сценария принятия/отзыва — 2 passed. Итоговый статус — **GO для локального релизного коммита**, без разрешения на push/публикацию. Предыдущие решения NO-GO ниже отражают состояние до исправления и сохранены как история. См. [обзор безопасности зависимостей](docs/DEPENDENCY_SECURITY_REVIEW.md).


> Исторический обзор исходного дерева этапа 10D до обновления зависимостей.
> Текущая повторная приёмка, изменённые версии и остаточный блокер Pillow:
> [DEPENDENCY_SECURITY_REVIEW.md](docs/DEPENDENCY_SECURITY_REVIEW.md).
> Текущие SHA-256 находятся в PUBLICATION_MANIFEST.json; прежние численность
> и результаты ниже описывают состояние до этого обновления.

21.10.2026, этап 10D. **Вердикт: NO-GO для публикации.** Публичное дерево подготовлено и функционально проверено, но проверка advisories выявила уязвимые версии зависимостей. Изменение зависимостей и повторная приёмка требуют отдельного решения владельца. Никаких commit, push, тегов, GitHub Release или изменений Pages не выполнено.

## 1. Основа и состав

Основа — свежий clone публичного репозитория [KirillTomenko/document-automation-assistant](https://github.com/KirillTomenko/document-automation-assistant), `main` / HEAD `d3a156c46700f1061789a4f0172e46b1a830551e`. HEAD и origin сохранены; индекс не содержит staged changes. История внутреннего проекта не переносилась, репозиторий не пересоздавался. `git fsck --full` проверяет публичную историю.

Публикационный набор: **165 файлов**. Все 163 пути принятого 2.0.0rc1 сохранены; добавлены этот обзор и [PUBLICATION_MANIFEST.json](docs/PUBLICATION_MANIFEST.json). Относительно текущего публичного main: **63 добавлено, 26 изменено, 3 удалено**, остальные файлы не меняются по Git diff. Полные списки — в разделе 8; SHA-256 каждого текущего файла, кроме самого manifest, — в новом manifest.

Относительно принятого кандидата изменены только README, docs/PUBLICATION.md, вводная исторического RC-отчёта и две строки версии: `pyproject.toml` и `src/document_automation/api.py`, 2.0.0rc1 → 2.0.0. Код обработки, схемы, тесты, scripts, fixtures, demo, зависимости, Dockerfile, compose.yaml и CI перенесены без изменения байтов. Новых функций нет.

`docs/RELEASE_MANIFEST.json` и основной текст `docs/RELEASE_CANDIDATE_REPORT.md` сохранены как историческое свидетельство этапа 10C; старый manifest описывает именно rc1 и не является перечнем текущих контрольных сумм. Для текущего дерева использовать PUBLICATION_MANIFEST.json.

## 2. Изменения относительно публичной версии

В public main переносится уже принятая функциональность кандидата: 27 сочетаний конечных структур PDF/DOCX/XLSX, проверяемое происхождение значений, оба DOCX, учёт попыток/usage/unknown/расчётной стоимости, идемпотентность, контролируемый повтор, журнал выпуска DOCX и восстановление, offline backup/verify/restore, принятие и отзыв результата, агрегированные бизнес-метрики. Исправления H01/H02 и Docker H03 сохранены.

Контракт work_request/1.0 сохранён; новые API/UI-запуски используют 2.0. Старые snapshots и fingerprints читаются без переписывания. Статус completed не подменяет needs_review или принятие оператором. Синтетические mock-суммы не являются расходами клиента.

## 3. Сохранённые публичные материалы

[Действующий GitHub Pages](https://kirilltomenko.github.io/document-automation-assistant/) и репозиторий вернули HTTP 200. Следующие файлы сохранены побайтово относительно принятого кандидата; по Git относительно public main не изменены. MP4/PNG также побайтово совпадают с publication-snapshot; HTML/SVG отличаются от него только CRLF/LF, их содержание совпадает:

| Путь | SHA-256 локального файла |
|---|---|
| `docs/index.html` | `52cf04d3cda11043bf712147e9323e2eab412d0d38721f1d363218035746eb48` |
| `docs/video/document-automation-assistant-demo.mp4` | `ecd722108bb5b6d86e0fe1b6412de0ee45959c0c50fbd7771801cc1c007ceb33` |
| `docs/video/document-automation-assistant-cover-play.png` | `424454bc35c5e812e78c86a7b37baab332c167c9ac9983ac38502877a0fc378c` |
| `docs/images/document-automation-assistant-flow.svg` | `b33f430434658eb81ad21320703abfb43f9e5ac8f346bb3bdd913bb446421a14` |

HTTP-проверка всех четырёх URL успешна: MP4 и PNG побайтово совпадают с выдачей Pages; HTML и SVG совпадают после нормализации CRLF/LF. Это различие переводов строк, не изменение содержания. Локальные src/poster/href страницы разрешаются. Другие прежние изображения и ссылки README также сохранены. Настройки Pages не менялись.

Старое видео обозначено как **базовый сценарий**, offline на синтетических данных. Новые функции 2.0 ему не приписываются. Предложение нового ролика/схемы/обложки остаётся только предложением; новые медиаматериалы не создавались.

## 4. Проверки именно подготовленного дерева

Новый Python 3.12 venv создан отдельно; system-site-packages выключены. Установка выполнялась из public-tree по README: `python -m pip --disable-pip-version-check install -e ".[test]"`, exit 0. Установлена именно 2.0.0. DAA_MODE=demo, DAA_ALLOW_PAID=false, реальный ключ не передавался. Платных LLM-вызовов нет.

| Проверка | Результат |
|---|---|
| Полная окончательная регрессия | **699 passed**, exit 0, 900.62 с; один прежний Starlette/httpx deprecation warning |
| pip check | exit 0, конфликтов зависимостей нет; это не проверка CVE |
| Ruff check / format | exit 0 / 0, 80 Python-файлов |
| scripts/check_repository.py / git diff --check | exit 0 / 0 |
| Контракты и форматы | В полной регрессии v1/v2, fingerprints, 27 offline + 27 mock-маршрутов, шесть перестановок каждого offline-комплекта, значения/состояния/provenance и два DOCX |
| Offline E2E | PPP, DDD, XXX, DXD, XPX: 5 completed, 10 DOCX, 0 LLM |
| Расходы, восстановление DOCX, backup/verify/restore, принятие | Пять опубликованных demo-скриптов завершились exit 0; метрики расходов используют mock |
| FastAPI / Streamlit | Реальный локальный запуск; оба health HTTP 200, A/B/C/V completed, T failed с блокировкой DOCX; 0 LLM |
| Изоляция исходников | Offline E2E с запретом чтения исходного рабочего проекта и старого RC, запретом внешних socket.connect; импорт из public-tree, 10 DOCX |
| Пакет | Собран wheel 2.0.0, встроенный DOCX-шаблон присутствует, runtime/секретных файлов нет; установка wheel и метаданные проверены |
| Секреты / документы / ссылки | См. разделы 5–6; расширенный scan содержимого, 35 PDF/Office-документов и публичная история; локальные ссылки разрешаются |
| Advisories зависимостей | **НЕ ПРОЙДЕНО**: 5 установленных пакетов с advisories, включая инструмент pip; раздел 6 |

Первый прогон не был успешным: 439 passed / 260 setup errors, exit 1. Причина — служебный `--basetemp=tmp/rc-regression` при отсутствующем родителе tmp (`FileNotFoundError`). После создания родителя полный прогон выполнен с новым basetemp. Исходники продукта и тесты не правились. Это ошибка команды проверки, а не основание скрыть её или считать первоначальный прогон успешным.

Первая сборка wheel в песочнице завершилась exit 1: WinError 5 при записи в пользовательский pip cache. Та же сборка вне песочницы завершилась exit 0. Оба результата сохранены. Изменения исходников для обхода ограничений среды не потребовались.

Повторные команды:

```powershell
python -m pip --disable-pip-version-check install -e ".[test]"
python -m pip check
python -B -m pytest -q -p no:cacheprovider
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -B scripts/check_repository.py
git diff --check
python -B scripts/demo_formats.py --output tmp/public-offline-demo
python -B scripts/demo_cost_metrics.py
python -B scripts/demo_artifact_recovery.py --output tmp/public-artifact-demo
python -B scripts/demo_backup_restore.py --output tmp/public-backup-demo
python -B scripts/demo_business_metrics.py --output tmp/public-business-demo
```

Для независимого повтора выбирать новые выходные каталоги. Полные фактические argv, stdout/stderr и exit codes сохранены рядом с деревом в локальном evidence, не в публикации. Проверки GitHub Actions на сервере не запускались, поскольку push запрещён.

## 5. Docker и H03

Из песочницы docker version/info получили отказ доступа к config/pipe; вне песочницы — успешные Client/Server 29.7.2, Docker Desktop 4.86.0, Linux/WSL2. Конфигурация H03 проверена повторно именно из public-tree с 2.0.0.

Все 42 зафиксированных Docker-команд завершились exit 0: config, build, новые named volumes, новый пустой Windows bind mount, UID/GID 1000:1000, writable /var/lib/daa и data, общий lock, SQLite integrity_check и записи runs, /health, синтетический комплект, генерация и скачивание двух DOCX, побайтовое совпадение после restart, idempotent replay, исключение maintenance при активном backend и разрешение после stop. Метаданные пакета и OpenAPI в образе — 2.0.0.

Compose-проект `daa-publication-20261021-10d`, API-порт `61117`, UI-порт `61118`: сборка обоих сервисов, health, пять сценариев, restart API, сохранено пять runs и 0 LLM. Проект использует только новые собственные volumes и сеть. Тестовые процессы/контейнеры остановлены; ресурсы сохранены для разбора. Прежние 11 контейнеров (5 running, 6 ранее stopped), образы, networks и volumes сохранены. Prune, global cleanup, root runtime и рабочие данные не использовались.

H03 остаётся закрытым в проверенной среде. Это не гарантирует права любого прежнего volume или произвольные Windows ACL. Инструкция обновления и ограничения: [DOCKER_SMOKE.md](docs/DOCKER_SMOKE.md). Синтетический прежний layout был проверен в 10C; в 10D повторены named/bind/Compose, конфигурация перенесена без изменений.

## 6. Безопасность, исключения и блокер

Содержимое всех 165 публикационных файлов проверено: UTF-8/бинарные строки, распакованные XML/rels DOCX/XLSX, текст/metadata PDF и видимый текст 35 документов. Проверены 106 blob публичной Git-истории. Единственный credential-pattern — намеренный фиктивный user:password URL в tests/test_proxyapi_smoke.py, негативный тест. Секретные ключи, реальные контакты/документы и локальные пути профиля в добавляемых файлах не обнаружены. Demo/fixtures и metadata синтетические; сохранённые media проверены по хешам принятого публичного материала.

В текущей поставке нет .env, ключей, рабочих SQLite, backup, диагностических дампов, venv, build/egg-info, tmp, SESSION_HANDOFF.md, AGENTS.md и PROJECT_BRIEF.md. Последние три файла уже находились в публичном main: их удаление из нового дерева намеренное; старая публичная история сохраняет прежние версии. Это не удаление из истории и не перенос внутренней истории разработки.

SHA-256 неизменённого исходного SESSION_HANDOFF.md: `0c16bf4c6f0c8d4aaa02a6bd85b75512facc42499a42167a04dd3e5c059a0a0c`. Исходные 159 проектных файлов, Git-файлы, весь publication-snapshot и весь принятый candidate сверены с baseline; изменений нет. Игнорируемые рабочие данные исходного проекта не копировались и не изменялись.

### Блокер DEP-01: известные advisories зависимостей

Проверены метаданные всех 64 установленных дистрибутивов: локальный проект исключён из запросов, 63 PyPI version JSON API вернули ответ без ошибок. Поле vulnerabilities содержит записи для пяти пакетов. Счётчики ниже — **записи advisories, не число уникальных CVE**; GHSA/PYSEC могут дублировать одну проблему. Эксплуатация каждой записи в этом MVP не доказывалась, вредоносные документы не запускались.

| Пакет | Роль | Версия | Записей | Исправленные версии, указанные источником |
|---|---|---|---|---|
| pillow | Транзитивная runtime-зависимость | 12.3.0 | 8 | 13.0.0 |
| pip | Инструмент нового venv; не зависимость приложения | 24.3.1 | 12 | 25.3, 26.0, 26.1, 26.1.2, 26.2, 26.2.0 |
| pyarrow | Транзитивная runtime-зависимость | 21.0.0 | 2 | 23.0.1 |
| pypdf | Прямая runtime-зависимость | 6.17.0 | 14 | 6.18.0, 6.18.1, 6.19.0 |
| streamlit | Прямая runtime-зависимость | 1.51.0 | 4 | 1.53.1, 1.54.0 |

Особенно существенны pypdf/извлечение текста: [GHSA-fp3h-c4fm-7vvf](https://github.com/py-pdf/pypdf/security/advisories/GHSA-fp3h-c4fm-7vvf) и [GHSA-g9cg-prrw-2r8q](https://github.com/py-pdf/pypdf/security/advisories/GHSA-g9cg-prrw-2r8q) описывают чрезмерный расход памяти при разборе шрифтов; исправления указаны в 6.18.1. Другие записи требуют 6.19.0. Ограничение размера загрузки не доказывает защиту от затрат CPU/RAM внутри PDF-парсера.

Streamlit: [advisory разработчика GHSA-7p48-42j8-8846](https://github.com/streamlit/streamlit/security/advisories/GHSA-7p48-42j8-8846), Windows SSRF/NTLM, исправлено в 1.54.0. Привязка localhost ограничивает доступ, а Linux Docker не затронут именно Windows-вектором; это не устраняет необходимость пересмотра зависимости для публичных Windows-инструкций. Pillow/PyArrow требуют отдельной проверки применимости и совместимых исправлений.

Ни одна зависимость не обновлялась в рамках 10D. Успех 699 тестов и Docker не отменяет DEP-01. Нет заявления об отсутствии всех уязвимостей: это проверка известных advisories установленных Python-пакетов, не аудит ОС образа, penetration test или доказательство безопасности всех путей. Прямые зависимости закреплены, но транзитивные версии и базовый Docker tag не зафиксированы полным hash-lock; фактический состав сохранён в evidence.

## 7. README и оставшиеся действия

README изложен для заказчика: ручная задача, одинаковые/смешанные форматы, конечные структуры и 27 сочетаний, два DOCX, происхождение и конфликты, needs_review, расчётная стоимость и unknown, безопасные повторы, backup/restore, подтверждённые проверки, локальный offline-запуск и Docker. Offline, mock и реальные платные запросы явно разделены; ограничения реальной LLM на v2 и локального MVP сохранены. Статус NO-GO указан до инструкций запуска.

До публикации необходимо:

1. Согласовать отдельное исправление DEP-01: проверить применимость всех advisories, подобрать совместимые исправленные версии прямых и транзитивных зависимостей и актуальный инструмент установки. Не считать перечисленные версии автоматически одобренным набором.
2. После изменения зависимостей повторить чистую установку, полную регрессию, Ruff, repository/secret/advisory checks, offline E2E, сборку и фактический Docker smoke; обновить manifest и этот отчёт.
3. Получить согласование окончательного diff и GO. Перед commit повторно проверить текущий public main; если он изменился, безопасно объединить новые публичные изменения и повторить затронутые проверки без force push.
4. Только после явного разрешения создать commit с согласованным набором файлов и обычный push; затем проверить GitHub Actions и прежние Pages/media URL. Тег v2.0.0 и GitHub Release — только при отдельном разрешении. Настройки Pages и старые media менять не требуется.

Подготовка 10D остановлена для решения владельца. До закрытия DEP-01 публиковать это дерево нельзя.

## 8. Точный diff относительно public main

### Добавлено: 63

- `PUBLICATION_DIFF_REVIEW.md`
- `config/llm_tariffs.example.json`
- `docs/ARTIFACT_RECOVERY.md`
- `docs/BACKUP_AND_RECOVERY.md`
- `docs/BUSINESS_METRICS.md`
- `docs/COST_METRICS.md`
- `docs/DEMO_CONTRACT_V2.md`
- `docs/DEMO_V2_PROPOSAL.md`
- `docs/DOCKER_SMOKE.md`
- `docs/DOCKER_VERIFICATION_RECORD.md`
- `docs/FINAL_ARCHITECTURE_AUDIT.md`
- `docs/FORMATS_V2.md`
- `docs/PUBLICATION_MANIFEST.json`
- `docs/RELEASE_CANDIDATE_REPORT.md`
- `docs/RELEASE_MANIFEST.json`
- `docs/SAFE_RETRIES_AND_IDEMPOTENCY.md`
- `scripts/build_format_fixtures.py`
- `scripts/demo_artifact_recovery.py`
- `scripts/demo_backup_restore.py`
- `scripts/demo_business_metrics.py`
- `scripts/demo_cost_metrics.py`
- `scripts/demo_formats.py`
- `scripts/smoke_docker.py`
- `src/document_automation/acceptance.py`
- `src/document_automation/artifact_files.py`
- `src/document_automation/backup.py`
- `src/document_automation/backup_paths.py`
- `src/document_automation/business_metrics.py`
- `src/document_automation/data_lease.py`
- `src/document_automation/extraction_v2.py`
- `src/document_automation/llm_usage.py`
- `src/document_automation/metering.py`
- `src/document_automation/run_requests.py`
- `src/document_automation/ui_business.py`
- `src/document_automation/workflows/work_request_v2_context.py`
- `src/document_automation/workflows/work_request_v2_processing.py`
- `tests/fixtures/formats/README.md`
- `tests/fixtures/formats/all.docx`
- `tests/fixtures/formats/all.pdf`
- `tests/fixtures/formats/all.xlsx`
- `tests/fixtures/formats/ambiguous.pdf`
- `tests/fixtures/formats/customer.docx`
- `tests/fixtures/formats/customer.pdf`
- `tests/fixtures/formats/customer.xlsx`
- `tests/fixtures/formats/description.docx`
- `tests/fixtures/formats/description.pdf`
- `tests/fixtures/formats/description.xlsx`
- `tests/fixtures/formats/items.docx`
- `tests/fixtures/formats/items.pdf`
- `tests/fixtures/formats/items.xlsx`
- `tests/fixtures/formats/unsupported.pdf`
- `tests/fixtures/formats/wrapped.pdf`
- `tests/test_artifact_recovery.py`
- `tests/test_artifact_recovery_boundaries.py`
- `tests/test_audit_regressions.py`
- `tests/test_backup.py`
- `tests/test_backup_boundaries.py`
- `tests/test_business_metrics.py`
- `tests/test_contract_v2.py`
- `tests/test_docker_configuration.py`
- `tests/test_formats_v2.py`
- `tests/test_llm_usage.py`
- `tests/test_run_requests.py`

### Изменено: 26

- `.gitignore`
- `Dockerfile`
- `README.md`
- `compose.yaml`
- `docs/API.md`
- `docs/BUSINESS_LOGIC.md`
- `docs/DECISIONS.md`
- `docs/DEMO.md`
- `docs/DEMO_CONTRACT.md`
- `docs/EXTRACTION.md`
- `docs/PUBLICATION.md`
- `docs/STORAGE.md`
- `pyproject.toml`
- `scripts/smoke_demo.py`
- `src/document_automation/adapters/openai_extractor.py`
- `src/document_automation/api.py`
- `src/document_automation/contracts.py`
- `src/document_automation/extraction.py`
- `src/document_automation/generation.py`
- `src/document_automation/schemas.py`
- `src/document_automation/service.py`
- `src/document_automation/storage.py`
- `src/document_automation/ui.py`
- `src/document_automation/ui_text.py`
- `src/document_automation/workflows/work_request.py`
- `tests/test_extraction.py`

### Удалено намеренно: 3

- `AGENTS.md`
- `PROJECT_BRIEF.md`
- `SESSION_HANDOFF.md`

## 9. Точные идентификаторы dependency advisories

### pillow 12.3.0

Источник: [PyPI version metadata](https://pypi.org/pypi/pillow/12.3.0/json).

- [GHSA-p4wg-p73j-c528](https://osv.dev/vulnerability/GHSA-p4wg-p73j-c528); исправлено: 13.0.0.
- [GHSA-65qm-jvmh-6pqg](https://osv.dev/vulnerability/GHSA-65qm-jvmh-6pqg); исправлено: 13.0.0.
- [GHSA-58r4-6rv3-gfh8](https://osv.dev/vulnerability/GHSA-58r4-6rv3-gfh8); исправлено: 13.0.0.
- [GHSA-pchr-8j38-3x4p](https://osv.dev/vulnerability/GHSA-pchr-8j38-3x4p); исправлено: 13.0.0.
- [GHSA-q45j-58j2-45rg](https://osv.dev/vulnerability/GHSA-q45j-58j2-45rg); исправлено: 13.0.0.
- [GHSA-462x-pgg6-55fp](https://osv.dev/vulnerability/GHSA-462x-pgg6-55fp); исправлено: 13.0.0.
- [GHSA-c6hw-5grf-ff96](https://osv.dev/vulnerability/GHSA-c6hw-5grf-ff96); исправлено: 13.0.0.
- [GHSA-vx6w-vv9f-6gvg](https://osv.dev/vulnerability/GHSA-vx6w-vv9f-6gvg); исправлено: 13.0.0.

### pip 24.3.1

Источник: [PyPI version metadata](https://pypi.org/pypi/pip/24.3.1/json).

- [GHSA-4xh5-x5gv-qwph](https://osv.dev/vulnerability/GHSA-4xh5-x5gv-qwph); исправлено: 25.3.
- [GHSA-6vgw-5pg2-w6jp](https://osv.dev/vulnerability/GHSA-6vgw-5pg2-w6jp); исправлено: 26.0.
- [GHSA-58qw-9mgm-455v](https://osv.dev/vulnerability/GHSA-58qw-9mgm-455v); исправлено: 26.1.
- [GHSA-jp4c-xjxw-mgf9](https://osv.dev/vulnerability/GHSA-jp4c-xjxw-mgf9); исправлено: 26.1.
- [PYSEC-2026-196](https://osv.dev/vulnerability/PYSEC-2026-196); исправлено: 26.1.2.
- [PYSEC-2026-1795](https://osv.dev/vulnerability/PYSEC-2026-1795); исправлено: 25.3.
- [PYSEC-2026-1796](https://osv.dev/vulnerability/PYSEC-2026-1796); исправлено: 26.0.
- [GHSA-wf93-45jw-7689](https://osv.dev/vulnerability/GHSA-wf93-45jw-7689); исправлено: 26.1.2.
- [PYSEC-2026-2875](https://osv.dev/vulnerability/PYSEC-2026-2875); исправлено: 26.1.
- [PYSEC-2026-2876](https://osv.dev/vulnerability/PYSEC-2026-2876); исправлено: 26.1.
- [PYSEC-2026-3721](https://osv.dev/vulnerability/PYSEC-2026-3721); исправлено: 26.2.
- [GHSA-qwm4-qh6w-59xr](https://osv.dev/vulnerability/GHSA-qwm4-qh6w-59xr); исправлено: 26.2.0.

### pyarrow 21.0.0

Источник: [PyPI version metadata](https://pypi.org/pypi/pyarrow/21.0.0/json).

- [PYSEC-2026-113](https://osv.dev/vulnerability/PYSEC-2026-113); исправлено: 23.0.1.
- [GHSA-rgxp-2hwp-jwgg](https://osv.dev/vulnerability/GHSA-rgxp-2hwp-jwgg); исправлено: 23.0.1.

### pypdf 6.17.0

Источник: [PyPI version metadata](https://pypi.org/pypi/pypdf/6.17.0/json).

- [GHSA-w23x-9jrw-r45c](https://osv.dev/vulnerability/GHSA-w23x-9jrw-r45c); исправлено: 6.19.0.
- [GHSA-fp3h-c4fm-7vvf](https://osv.dev/vulnerability/GHSA-fp3h-c4fm-7vvf); исправлено: 6.18.1.
- [GHSA-5jq2-8x83-x246](https://osv.dev/vulnerability/GHSA-5jq2-8x83-x246); исправлено: 6.18.0.
- [GHSA-php9-fj8v-98fj](https://osv.dev/vulnerability/GHSA-php9-fj8v-98fj); исправлено: 6.19.0.
- [GHSA-g9cg-prrw-2r8q](https://osv.dev/vulnerability/GHSA-g9cg-prrw-2r8q); исправлено: 6.18.1.
- [GHSA-jw7q-gvrg-4vj3](https://osv.dev/vulnerability/GHSA-jw7q-gvrg-4vj3); исправлено: 6.18.1.
- [GHSA-v247-6f48-mgcj](https://osv.dev/vulnerability/GHSA-v247-6f48-mgcj); исправлено: 6.19.0.
- [PYSEC-2026-4153](https://osv.dev/vulnerability/PYSEC-2026-4153); исправлено: 6.18.0.
- [PYSEC-2026-4154](https://osv.dev/vulnerability/PYSEC-2026-4154); исправлено: 6.18.1.
- [PYSEC-2026-4160](https://osv.dev/vulnerability/PYSEC-2026-4160); исправлено: 6.19.0.
- [PYSEC-2026-4159](https://osv.dev/vulnerability/PYSEC-2026-4159); исправлено: 6.19.0.
- [PYSEC-2026-4156](https://osv.dev/vulnerability/PYSEC-2026-4156); исправлено: 6.18.1.
- [PYSEC-2026-4155](https://osv.dev/vulnerability/PYSEC-2026-4155); исправлено: 6.18.1.
- [PYSEC-2026-4157](https://osv.dev/vulnerability/PYSEC-2026-4157); исправлено: 6.19.0.

### streamlit 1.51.0

Источник: [PyPI version metadata](https://pypi.org/pypi/streamlit/1.51.0/json).

- [GHSA-7p48-42j8-8846](https://osv.dev/vulnerability/GHSA-7p48-42j8-8846); исправлено: 1.54.0.
- [PYSEC-2026-212](https://osv.dev/vulnerability/PYSEC-2026-212); исправлено: 1.53.1.
- [PYSEC-2026-2285](https://osv.dev/vulnerability/PYSEC-2026-2285); исправлено: 1.54.0.
- [GHSA-vqwp-45wm-r9r5](https://osv.dev/vulnerability/GHSA-vqwp-45wm-r9r5); исправлено: 1.53.1.
