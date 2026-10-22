# Dependency Security Review — Document Automation Assistant 2.0.0


> **Итоговое обновление 22.10.2026 (этап 10F): GO для локальной поставки.** Проверены Streamlit 1.66.1, Pillow 13.0.0 и pypdf 6.20.1; pip check успешен, pip-audit сообщил 0 известных уязвимостей проверенных зависимостей, полная регрессия — 699 passed, Ruff прошёл, фактический Docker smoke и два Streamlit AppTest прошли. Разделы ниже фиксируют состояние на 21.10 и являются историей, а не текущим блокером.

21.10.2026. **NO-GO для публикации.** Указанные pypdf/Streamlit advisories и дополнительные находки PyArrow/pip устранены; сборочный backend закреплён на версии без находок. Остаются восемь advisories Pillow 12.3.0. Исправленная Pillow 13.0.0 несовместима с официальным ограничением Streamlit `<13`; pip подтвердил конфликт. Ни ошибки, ни advisories не исключались из проверки.

## 1. Область работ и исходное состояние

Изменено только изолированное подготовленное публичное дерево на public main `d3a156c46700f1061789a4f0172e46b1a830551e`. Исходный рабочий проект, старый publication-snapshot и принятый rc1 не изменялись. Commit, push, тег, Release, публикация и платные LLM-вызовы не выполнялись. Код продукта, бизнес-контракты, все существующие тесты, scripts и синтетические документы сохранены побайтово относительно принятого дерева 10D. Новых функций и исправлений бизнес-логики нет.

Исходные версии проверены через `pip list --format=json` в действительном venv этапа 10D. Для повторной приёмки создан отдельный чистый Python 3.12 venv без system-site-packages; старое окружение не обновлялось. Новые версии дополнительно получены через pip list внутри собранного Docker-образа.

## 2. Подтверждение исходных advisories и выбор версий

[pypdf GHSA-fp3h-c4fm-7vvf](https://github.com/py-pdf/pypdf/security/advisories/GHSA-fp3h-c4fm-7vvf), CVE-2026-102995: чрезмерное потребление памяти при обработке ToUnicode во время извлечения текста, исправлено с **6.18.1**. Выбрана **6.19.0**, поскольку дополнительные advisories установленной 6.17.0 требуют также 6.19.0.

[Streamlit GHSA-7p48-42j8-8846](https://github.com/streamlit/streamlit/security/advisories/GHSA-7p48-42j8-8846), CVE-2026-33682: Windows SSRF/NTLM через обработку путей, исправлено с **1.54.0**. Выбрана именно 1.54.0 как минимальная версия для указанного исправления; произвольного обновления UI до последней версии нет.

| Компонент | До, фактический Windows venv 10D | После, Windows / Docker | Решение |
|---|---|---|---|
| pypdf | 6.17.0 | 6.19.0 / 6.19.0 | Закреплено в pyproject.toml и constraints |
| Streamlit | 1.51.0 | 1.54.0 / 1.54.0 | Закреплено в pyproject.toml и constraints |
| PyArrow | 21.0.0 | 23.0.1 / 23.0.1 | Транзитивная зависимость, минимальное исправление advisory; закреплена constraints |
| pip | 24.3.1 | 26.2 / 26.2 | Исправленный инструмент установки |
| setuptools | В постоянном venv отсутствовал; build-system разрешал >=80,<81 | build-system ==83.0.0 | Исправление сборочного диапазона и точное закрепление |
| Pillow | 12.3.0 | 12.3.0 / 12.3.0 | Остаётся блокером; несовместимая 13.0.0 не устанавливалась принудительно |

Остальные версии сохранены из исходного окружения. [constraints-py312.txt](../constraints-py312.txt) содержит 63 точных версии, включая инструменты установки/сборки. Все 62 сторонние зависимости чистого Windows venv и 56 сторонних зависимостей runtime Docker совпали с constraints; незакреплённых установленных пакетов нет. Разница наборов включает тестовые и платформенные зависимости. Локальный пакет приложения учитывается отдельно, поскольку это проверяемый исходный проект, а не сторонний пакет из PyPI.

Сборка использует setuptools==83.0.0. [Advisory setuptools](https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c) относится к включению исключённых файлов в sdist при Unicode-нормализации на macOS; GitHub-страница всё ещё показывает Patched versions: None, тогда как [PyPI 80.9.0 metadata](https://pypi.org/pypi/setuptools/80.9.0/json) указывает исправление 83.0.0, и аудит закреплённой 83.0.0 находок не даёт. Это расхождение источников сохранено явно; здесь собирается wheel, не macOS sdist.

## 3. Фактический pip-audit

Использован **pip-audit 2.10.1** в отдельном служебном venv, сервис PyPI. Входы — полный список фактически установленных сторонних пакетов, а не только pyproject. Дополнительно проверены все constraints, включая build backend. Не применялись --ignore-vuln, --skip-editable, фильтрация результатов или отключение предупреждений. Локальный пакет приложения не отправляется на поиск в PyPI; все сторонние зависимости включены.

Команда для каждого из сохранённых полных списков:

```powershell
python -m pip_audit -r installed-requirements.txt --no-deps --disable-pip --progress-spinner off --format json --output audit.json
```

`--no-deps --disable-pip` означают проверку уже полного закреплённого списка без повторного разрешения зависимостей; это не исключение транзитивных пакетов. Список сопоставлен с pip list, constraints и pip check. Все запросы завершились, skipped packages = 0.

| Аудит | Сторонних пакетов | Результат | Exit |
|---|---|---|---|
| Исходный Windows venv | 62 | 33 записи в 5 пакетах, включая повторяющиеся алиасы | 1 |
| Новый чистый Windows venv | 62 | 8 advisories, только Pillow 12.3.0 | 1 |
| Новый Docker runtime | 56 | 8 advisories, только Pillow 12.3.0 | 1 |
| Все закреплённые constraints, включая setuptools | 63 | 8 advisories, только Pillow 12.3.0 | 1 |

Восемь остаточных записей соответствуют восьми отдельным CVE. Исходные 33 записи нельзя считать 33 уникальными уязвимостями: сервис возвращает дублирующиеся алиасы. Полные JSON, stderr, команды и exit codes сохранены в локальном evidence вне поставки.

Сканер выдал два предупреждения: `--no-deps is supported, but users are encouraged to fully hash their pinned dependencies` и рекомендацию pip-compile/use hashes. Они не скрыты. Constraints фиксирует версии, но не все допустимые wheel/sdist-хеши: это воспроизводимость выбора версий в проверенных Python 3.12 Windows/Linux, не обещание идентичного бинарного артефакта на любой платформе. SHA-256 собственной поставки и собранного wheel зафиксированы отдельно. Полный hash-lock сторонних дистрибутивов рекомендуется как последующее усиление поставки; текущий блокер — известные уязвимости Pillow.

## 4. Дополнительные находки и остаточный риск

| Группа | Критичность по GitHub Advisory Database API | Исправление и применимость |
|---|---|---|
| Дополнительные pypdf advisories | High; исходные страницы части записей обозначают Moderate | 6.19.0; проблемы CPU/RAM при разборе PDF релевантны загрузке документов |
| PyArrow GHSA-rgxp-2hwp-jwgg | High | 23.0.1; use-after-free при чтении определённых Arrow IPC файлов с pre-buffering. Прямого IPC-upload в MVP нет; транзитивный пакет всё равно обновлён |
| pip advisories | Low / Medium | 26.2; относятся к инструменту установки, не бизнес-процессу |
| setuptools GHSA-h35f-9h28-mq5c | Medium | Закреплён 83.0.0; расхождение источников и границы описаны выше |
| Pillow, восемь advisories | Шесть High, две Medium | Исправления указаны в 13.0.0; официальный Streamlit ограничивает Pillow <13 |

Критичность относится к пакету согласно API базы GitHub, а не к доказанной эксплуатации конкретного приложения. Для pypdf severity API и отображение upstream-страниц различаются; риск не занижался из-за этого расхождения.

### Почему Pillow не закрыта

[Метаданные Streamlit 1.54.0](https://pypi.org/pypi/streamlit/1.54.0/json) требуют pillow>=7.1.0,<13. [Текущая 1.66.0](https://pypi.org/pypi/streamlit/1.66.0/json) также требует <13. Проверены более ранние Pillow 10.2–12.3: они тоже имеют advisories; простой downgrade не даёт безопасного решения. Для примера [GHSA-p4wg-p73j-c528](https://github.com/python-pillow/Pillow/security/advisories/GHSA-p4wg-p73j-c528) охватывает 5.1.0–12.3.0 и исправлена в 13.0.0.

Разрешение зависимостей проверено без установки:

```powershell
python -m pip --disable-pip-version-check install --dry-run --ignore-installed --no-cache-dir streamlit==1.54.0 pillow==13.0.0
```

Фактический результат — **ResolutionImpossible, exit 1**. Metadata пакетов не переписывалась, несовместимая комбинация через --no-deps не устанавливалась, форк/backport не создавался.

В src/scripts приложения нет вызовов PIL/Image.open/Image.save или st.image; загрузка ограничена PDF/DOCX/XLSX, без OCR и обработки пользовательских растровых изображений. Достижимость уязвимых Pillow-операций из штатного workflow не установлена, вредоносные PoC не запускались. Это снижает наблюдаемую поверхность применения, но не устраняет установленную уязвимую зависимость и не даёт GO по заданному критерию.

Остаточные advisories:

| GHSA / CVE | Severity | Исправлено |
|---|---|---|
| [GHSA-p4wg-p73j-c528](https://github.com/advisories/GHSA-p4wg-p73j-c528) / CVE-2026-112895 | high | 13.0.0 |
| [GHSA-65qm-jvmh-6pqg](https://github.com/advisories/GHSA-65qm-jvmh-6pqg) / CVE-2026-112400 | high | 13.0.0 |
| [GHSA-58r4-6rv3-gfh8](https://github.com/advisories/GHSA-58r4-6rv3-gfh8) / CVE-2026-112397 | high | 13.0.0 |
| [GHSA-pchr-8j38-3x4p](https://github.com/advisories/GHSA-pchr-8j38-3x4p) / CVE-2026-112896 | high | 13.0.0 |
| [GHSA-q45j-58j2-45rg](https://github.com/advisories/GHSA-q45j-58j2-45rg) / CVE-2026-112897 | high | 13.0.0 |
| [GHSA-462x-pgg6-55fp](https://github.com/advisories/GHSA-462x-pgg6-55fp) / CVE-2026-112395 | medium | 13.0.0 |
| [GHSA-c6hw-5grf-ff96](https://github.com/advisories/GHSA-c6hw-5grf-ff96) / CVE-2026-112404 | high | 13.0.0 |
| [GHSA-vx6w-vv9f-6gvg](https://github.com/advisories/GHSA-vx6w-vv9f-6gvg) / CVE-2026-112899 | medium | 13.0.0 |

## 5. Повторная функциональная приёмка

Полная регрессия нового чистого окружения: **699 passed**, exit 0, 781.12 с по оболочке проверки. Один прежний Starlette/httpx deprecation warning сохранён, не подавлялся. Ни один тест не удалён, не ослаблен и не пропущен ради обновления. Новых продуктовых тестов не добавлялось: несовместимости поведения не выявлены, а изменённые поверхности уже покрываются существующей регрессией. Дополнительно выполнены аудит фактических зависимостей, проверка constraints и отрицательный resolver-probe.

| Область | Фактическая проверка |
|---|---|
| 27 сочетаний | 27 offline + 27 mock HTTP маршрутов, шесть перестановок offline; значения, состояния, provenance, оба DOCX |
| PDF и v1/v2 | Новый pypdf проходит существующие парсинг/пограничные тесты; старые v1 snapshots и fingerprints читаются |
| Streamlit | Существующие AppTest: upload/review, безопасный повтор, recovery, принятие и отзыв; отдельный живой HTTP health и A/B/C/V/T smoke |
| Расходы/попытки/идемпотентность | Существующие тесты и синтетический demo_cost_metrics; unknown не превращается в ноль |
| Backup/recovery/принятие | demo_artifact_recovery, demo_backup_restore, demo_business_metrics: exit 0 |
| Offline E2E | PPP, DDD, XXX, DXD, XPX: 5 completed, 10 DOCX, без LLM |
| Ruff / репозиторий | check и format: exit 0; check_repository.py и git diff --check: exit 0 |
| Установка/пакет | Чистая установка из подготовленного дерева с constraints и pip check: exit 0; новый wheel собран с setuptools 83.0.0 |

Бизнес-контракты, код продукта и тесты побайтово совпадают с деревом до обновления. Новые версии пакетов не изменили проверенные результаты PDF и интерфейса. Реальные платные API не проверялись и не вызывались.

## 6. Docker

Собраны новые изолированные образы из обновлённого Dockerfile. 43 Docker-команд завершились exit 0. Named volumes, новый Windows bind mount и Compose-проект `daa-security-20261021` (API 55189, UI 55190) проверены отдельно.

Подтверждены UID/GID 1000:1000, запись в /var/lib/daa/data и writable parent, создание shared lock, SQLite integrity_check и записи runs, FastAPI /health, обработка синтетики, генерация/скачивание двух DOCX, побайтовое совпадение после restart, тот же run_id при повторе Idempotency-Key, отсутствие LLM-попыток, блокировка maintenance во время backend и разрешение после его остановки. Compose прошёл пять сценариев и сохранил результаты после restart API.

Все прежние 15 контейнеров, включая пять пользовательских running, а также прежние образы, volumes и networks сохранены. Остановлены только созданные этим прогоном контейнеры; тестовые ресурсы оставлены для разбора. Runtime от root, prune и глобальная очистка не применялись. H03 остаётся закрытым в проверенной среде. Аудит Python-зависимостей образа не является аудитом всех OS-пакетов базового python:3.12-slim; digest базового образа не закреплялся.

## 7. Поставка и публичные материалы

Изменения ограничены pyproject.toml, constraints-py312.txt, командами установки Docker/CI/README и отчётами/manifest. Git HEAD и публичная история сохранены. Рабочие SQLite, backup, venv, диагностические материалы и SESSION_HANDOFF.md в публикационный набор не входят.

Итоговый набор — 167 файлов: добавлены constraints-py312.txt и этот отчёт; изменены pyproject.toml, Dockerfile, .github/workflows/ci.yml, README.md, PUBLICATION_DIFF_REVIEW.md, docs/PUBLICATION.md и docs/PUBLICATION_MANIFEST.json. Удалённых файлов нет. Сборочный мусор и результаты тестов перенесены из публичного дерева в отдельный локальный evidence. Подготовлены проверочный ZIP и wheel; их SHA-256 находятся в отдельном final-seal.json.

Повторная проверка охватила содержимое всех 167 файлов, XML/metadata DOCX/XLSX, текст и metadata PDF, 35 синтетических документов и 106 blob публичной истории. Запрещённых путей и новых секретов не найдено. Единственное срабатывание credential_url — существующий фиктивный user:password в tests/test_proxyapi_smoke.py, проверяющий отказ от такого URL; оно сохранено в отчёте сканера. Это проверка шаблонов и содержания, не математическое доказательство отсутствия любых секретов. 91 локальная ссылка корректна; четыре URL Pages/материалов и публичный репозиторий ответили HTTP 200. Git diff --check, git fsck и check_repository.py прошли после подготовки документов.

Сохранены побайтово относительно подготовленного дерева 10D:

| Путь | SHA-256 |
|---|---|
| `docs/index.html` | `52cf04d3cda11043bf712147e9323e2eab412d0d38721f1d363218035746eb48` |
| `docs/video/document-automation-assistant-demo.mp4` | `ecd722108bb5b6d86e0fe1b6412de0ee45959c0c50fbd7771801cc1c007ceb33` |
| `docs/video/document-automation-assistant-cover-play.png` | `424454bc35c5e812e78c86a7b37baab332c167c9ac9983ac38502877a0fc378c` |
| `docs/images/document-automation-assistant-flow.svg` | `b33f430434658eb81ad21320703abfb43f9e5ac8f346bb3bdd913bb446421a14` |

Действующий адрес [GitHub Pages](https://kirilltomenko.github.io/document-automation-assistant/) сохранён. MP4 и обложка совпадают также с publication-snapshot и HTTP-выдачей; HTML/SVG отличаются от snapshot/HTTP только CRLF/LF. Старое видео по-прежнему обозначено как базовый offline-сценарий. Новых видео/иллюстраций и изменений GitHub Pages нет.

## 8. Ошибки среды, ограничения и вердикт

Начальные установки pip/pip-audit и установка build-зависимости wheel в песочнице перестали продвигаться. Остановлены только идентифицированные процессы текущей установки; результаты прерванных попыток сохранены. Stop-Process для дочернего процесса сборки вернул внутреннюю NullReferenceException, завершение того же проверенного PID через .NET прошло успешно. Повтор сборки и установок вне песочницы прошёл успешно. Отдельный запуск pip-audit в песочнице получил PermissionError к пользовательскому cache; повтор вне песочницы сформировал полный отчёт. Это не принималось за успешный аудит и не обходилось исключением уязвимостей. Root cause зависания pip не установлен.

**Исторический вердикт этапа 10E: NO-GO на 21.10.2026; superseded итогом 10F выше.**

На момент этапа 10E для GO требовался официальный совместимый набор Streamlit/Pillow с исправленными advisories либо отдельно согласованный и проверенный backport. Затем повторить чистую установку, полный аудит, 699 тестов и Docker smoke, обновить отчёты/manifest. Полный hash-lock сторонних дистрибутивов рекомендуется отдельно. Не заменять устранение уязвимостей принудительной установкой несовместимых версий или отключением находок сканера. До отдельного решения владельца публикация остановлена.

## Приложение: исправленные и рассмотренные advisories

Критичность и CVE получены из GitHub Advisory Database API; сведения о версиях дополнительно сопоставлены с PyPI и pip-audit.

| Компонент | GHSA / CVE | Severity | Статус |
|---|---|---|---|
| pip | [GHSA-4xh5-x5gv-qwph](https://github.com/advisories/GHSA-4xh5-x5gv-qwph) / CVE-2025-8869 | medium | Устранено в выбранной версии |
| pip | [GHSA-6vgw-5pg2-w6jp](https://github.com/advisories/GHSA-6vgw-5pg2-w6jp) / CVE-2026-1703 | low | Устранено в выбранной версии |
| pip | [GHSA-58qw-9mgm-455v](https://github.com/advisories/GHSA-58qw-9mgm-455v) / CVE-2026-3219 | medium | Устранено в выбранной версии |
| pip | [GHSA-jp4c-xjxw-mgf9](https://github.com/advisories/GHSA-jp4c-xjxw-mgf9) / CVE-2026-6357 | medium | Устранено в выбранной версии |
| pip | [GHSA-wf93-45jw-7689](https://github.com/advisories/GHSA-wf93-45jw-7689) / CVE-2026-8643 | medium | Устранено в выбранной версии |
| pip | [GHSA-qwm4-qh6w-59xr](https://github.com/advisories/GHSA-qwm4-qh6w-59xr) / CVE-2026-13346 | medium | Устранено в выбранной версии |
| pyarrow | [GHSA-rgxp-2hwp-jwgg](https://github.com/advisories/GHSA-rgxp-2hwp-jwgg) / CVE-2026-25087 | high | Устранено в выбранной версии |
| pypdf | [GHSA-w23x-9jrw-r45c](https://github.com/advisories/GHSA-w23x-9jrw-r45c) / CVE-2026-103000 | high | Устранено в выбранной версии |
| pypdf | [GHSA-fp3h-c4fm-7vvf](https://github.com/advisories/GHSA-fp3h-c4fm-7vvf) / CVE-2026-102995 | high | Устранено в выбранной версии |
| pypdf | [GHSA-5jq2-8x83-x246](https://github.com/advisories/GHSA-5jq2-8x83-x246) / CVE-2026-102994 | high | Устранено в выбранной версии |
| pypdf | [GHSA-php9-fj8v-98fj](https://github.com/advisories/GHSA-php9-fj8v-98fj) / CVE-2026-102998 | high | Устранено в выбранной версии |
| pypdf | [GHSA-g9cg-prrw-2r8q](https://github.com/advisories/GHSA-g9cg-prrw-2r8q) / CVE-2026-102996 | high | Устранено в выбранной версии |
| pypdf | [GHSA-jw7q-gvrg-4vj3](https://github.com/advisories/GHSA-jw7q-gvrg-4vj3) / CVE-2026-102997 | high | Устранено в выбранной версии |
| pypdf | [GHSA-v247-6f48-mgcj](https://github.com/advisories/GHSA-v247-6f48-mgcj) / CVE-2026-102999 | high | Устранено в выбранной версии |
| streamlit | [GHSA-7p48-42j8-8846](https://github.com/advisories/GHSA-7p48-42j8-8846) / CVE-2026-33682 | medium | Устранено в выбранной версии |
| streamlit | [GHSA-vqwp-45wm-r9r5](https://github.com/advisories/GHSA-vqwp-45wm-r9r5) / CVE-2026-10804 | low | Устранено в выбранной версии |
| setuptools | [GHSA-h35f-9h28-mq5c](https://github.com/advisories/GHSA-h35f-9h28-mq5c) / CVE-2026-59890 | medium | Нет находок в выбранной версии; оговорка источников выше |


## Этап 10F — итог повторной приёмки (22.10.2026)

- Фактически установленные версии в чистом Python 3.12 окружении: Streamlit 1.66.1, Pillow 13.0.0, pypdf 6.20.1. pip check: No broken requirements found.
- pip-audit --local: 0 известных уязвимостей в проверяемых сторонних пакетах. Аудитор сообщает, что локальный пакет приложения отсутствует на PyPI и поэтому не анализирует его как опубликованный пакет; исходники проверяются отдельно тестами и review.
- Полная регрессия после исправления относительного пути в тестах Streamlit: 699 passed; осталось одно предупреждение Starlette/httpx. Два затронутых сценария принятия/отзыва отдельно прошли (2 passed). Ruff check и Ruff format --check прошли.
- Docker образ собран с указанными версиями. Smoke подтвердил non-root UID/GID 1000, lock и SQLite, HTTP /health, синтетическую обработку, оба DOCX и их сохранность после рестарта; LLM-вызовов — 0.


Итог 10F: **GO для локального релизного коммита и отдельного рассмотрения публикации**. Это не разрешение на push, тег или GitHub Release.
