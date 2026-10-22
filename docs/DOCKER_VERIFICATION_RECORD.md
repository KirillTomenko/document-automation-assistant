# Фактические команды Docker 10C

Проверки 20–21.10.2026. Ниже журнал выполненных команд и кодов завершения,
а не инструкция повторно использовать сохранённые тестовые имена.
Для нового прогона используйте [smoke_docker.py](../scripts/smoke_docker.py): он выбирает новые имена.
Абсолютный путь Windows bind заменён маркером <TEMP>; полные команды сохранены в локальном evidence. Частные сведения docker info не публикуются.

Первое обращение внутри песочницы: docker version и docker info — exit 1,
отказ доступа к config/named pipe. Повтор вне песочницы — оба exit 0, Engine 29.7.2.

## Named volumes, Windows bind и Compose

1. Код завершения: **0**.

```powershell
& 'docker' 'ps' '-a' '--no-trunc' '--format' '{{json .}}'
```

2. Код завершения: **0**.

```powershell
& 'docker' 'image' 'ls' '--no-trunc' '--format' '{{.ID}} {{.Repository}}:{{.Tag}}'
```

3. Код завершения: **0**.

```powershell
& 'docker' 'volume' 'ls' '--format' '{{.Name}}'
```

4. Код завершения: **0**.

```powershell
& 'docker' 'network' 'ls' '--no-trunc' '--format' '{{.ID}} {{.Name}}'
```

5. Код завершения: **0**.

```powershell
& 'docker' 'version'
```

6. Код завершения: **0**.

```powershell
& 'docker' 'info'
```

7. Код завершения: **0**.

```powershell
& 'docker' 'compose' 'config' '--quiet'
```

8. Код завершения: **0**.

```powershell
& 'docker' 'version'
```

9. Код завершения: **0**.

```powershell
& 'docker' 'build' '--tag' 'daa-smoke-bccde37de2f8:local' '.'
```

10. Код завершения: **0**.

```powershell
& 'docker' 'volume' 'create' 'daa-smoke-bccde37de2f8-state'
```

11. Код завершения: **0**.

```powershell
& 'docker' 'volume' 'create' 'daa-smoke-bccde37de2f8-data'
```

12. Код завершения: **0**.

```powershell
& 'docker' 'run' '--detach' '--name' 'daa-smoke-bccde37de2f8' '--mount' 'type=volume,source=daa-smoke-bccde37de2f8-state,target=/var/lib/daa' '--mount' 'type=volume,source=daa-smoke-bccde37de2f8-data,target=/var/lib/daa/data' '--publish' '127.0.0.1::8000' '--env' 'DAA_MODE=demo' '--env' 'DAA_ALLOW_PAID=false' 'daa-smoke-bccde37de2f8:local'
```

13. Код завершения: **0**.

```powershell
& 'docker' 'port' 'daa-smoke-bccde37de2f8' '8000/tcp'
```

14. Код завершения: **0**.

```powershell
& 'docker' 'exec' 'daa-smoke-bccde37de2f8' 'python' '-c' 'import os; from pathlib import Path; assert os.geteuid() == os.getegid() == 1000; p=Path(os.environ[''DAA_DATA_DIR'']); assert p.as_posix() == ''/var/lib/daa/data''; assert os.access(p, os.W_OK) and os.access(p.parent, os.W_OK); assert len(list(p.parent.glob(''.daa-data-*.lock''))) == 1'
```

15. Код завершения: **0**.

```powershell
& 'docker' 'run' '--rm' '--network' 'none' '--mount' 'type=volume,source=daa-smoke-bccde37de2f8-state,target=/var/lib/daa' '--mount' 'type=volume,source=daa-smoke-bccde37de2f8-data,target=/var/lib/daa/data' 'daa-smoke-bccde37de2f8:local' 'python' '-c' 'import os
from document_automation.data_lease import DataLease, DataBusyError
assert os.geteuid() == os.getegid() == 1000
try:
    with DataLease(os.environ[''DAA_DATA_DIR''], ''maintenance''): pass
except DataBusyError:
    assert True, ''Unexpected busy lease''
else:
    assert False, ''Shared lock failed to exclude maintenance''
'
```

16. Код завершения: **0**.

```powershell
& 'docker' 'restart' 'daa-smoke-bccde37de2f8'
```

17. Код завершения: **0**.

```powershell
& 'docker' 'port' 'daa-smoke-bccde37de2f8' '8000/tcp'
```

18. Код завершения: **0**.

```powershell
& 'docker' 'stop' 'daa-smoke-bccde37de2f8'
```

19. Код завершения: **0**.

```powershell
& 'docker' 'run' '--rm' '--network' 'none' '--mount' 'type=volume,source=daa-smoke-bccde37de2f8-state,target=/var/lib/daa' '--mount' 'type=volume,source=daa-smoke-bccde37de2f8-data,target=/var/lib/daa/data' 'daa-smoke-bccde37de2f8:local' 'python' '-c' 'import os
from document_automation.data_lease import DataLease, DataBusyError
assert os.geteuid() == os.getegid() == 1000
try:
    with DataLease(os.environ[''DAA_DATA_DIR''], ''maintenance''): pass
except DataBusyError:
    assert False, ''Unexpected busy lease''
else:
    assert True, ''Shared lock failed to exclude maintenance''
'
```

20. Код завершения: **0**.

```powershell
& 'docker' 'version'
```

21. Код завершения: **0**.

```powershell
& 'docker' 'build' '--tag' 'daa-smoke-d0f6780b1ca1:local' '.'
```

22. Код завершения: **0**.

```powershell
& 'docker' 'run' '--detach' '--name' 'daa-smoke-d0f6780b1ca1' '--mount' 'type=bind,source=<TEMP>/daa-2.0-rc-20261020/docker-bind,target=/var/lib/daa' '--publish' '127.0.0.1::8000' '--env' 'DAA_MODE=demo' '--env' 'DAA_ALLOW_PAID=false' 'daa-smoke-d0f6780b1ca1:local'
```

23. Код завершения: **0**.

```powershell
& 'docker' 'port' 'daa-smoke-d0f6780b1ca1' '8000/tcp'
```

24. Код завершения: **0**.

```powershell
& 'docker' 'exec' 'daa-smoke-d0f6780b1ca1' 'python' '-c' 'import os; from pathlib import Path; assert os.geteuid() == os.getegid() == 1000; p=Path(os.environ[''DAA_DATA_DIR'']); assert p.as_posix() == ''/var/lib/daa/data''; assert os.access(p, os.W_OK) and os.access(p.parent, os.W_OK); assert len(list(p.parent.glob(''.daa-data-*.lock''))) == 1'
```

25. Код завершения: **0**.

```powershell
& 'docker' 'run' '--rm' '--network' 'none' '--mount' 'type=bind,source=<TEMP>/daa-2.0-rc-20261020/docker-bind,target=/var/lib/daa' 'daa-smoke-d0f6780b1ca1:local' 'python' '-c' 'import os
from document_automation.data_lease import DataLease, DataBusyError
assert os.geteuid() == os.getegid() == 1000
try:
    with DataLease(os.environ[''DAA_DATA_DIR''], ''maintenance''): pass
except DataBusyError:
    assert True, ''Unexpected busy lease''
else:
    assert False, ''Shared lock failed to exclude maintenance''
'
```

26. Код завершения: **0**.

```powershell
& 'docker' 'restart' 'daa-smoke-d0f6780b1ca1'
```

27. Код завершения: **0**.

```powershell
& 'docker' 'port' 'daa-smoke-d0f6780b1ca1' '8000/tcp'
```

28. Код завершения: **0**.

```powershell
& 'docker' 'stop' 'daa-smoke-d0f6780b1ca1'
```

29. Код завершения: **0**.

```powershell
& 'docker' 'run' '--rm' '--network' 'none' '--mount' 'type=bind,source=<TEMP>/daa-2.0-rc-20261020/docker-bind,target=/var/lib/daa' 'daa-smoke-d0f6780b1ca1:local' 'python' '-c' 'import os
from document_automation.data_lease import DataLease, DataBusyError
assert os.geteuid() == os.getegid() == 1000
try:
    with DataLease(os.environ[''DAA_DATA_DIR''], ''maintenance''): pass
except DataBusyError:
    assert False, ''Unexpected busy lease''
else:
    assert True, ''Shared lock failed to exclude maintenance''
'
```

30. Код завершения: **0**.

```powershell
& 'docker' 'compose' '-p' 'daa-rc-20261020-10c' 'build' 'api' 'ui'
```

31. Код завершения: **0**.

```powershell
& 'docker' 'compose' '-p' 'daa-rc-20261020-10c' 'up' '-d' 'api' 'ui'
```

32. Код завершения: **0**.

```powershell
& 'docker' 'compose' '-p' 'daa-rc-20261020-10c' 'ps' '-q' 'api'
```

33. Код завершения: **0**.

```powershell
& 'docker' 'exec' '14d60b7e211760d66329935cc21e580debcc4ad91f486c098d7526a342f3e05b' 'python' '-c' 'import os;from pathlib import Path;assert os.getuid()==os.getgid()==1000;p=Path(''/var/lib/daa'');assert os.access(p,os.W_OK);assert len(list(p.glob(''.daa-data-*.lock'')))==1'
```

34. Код завершения: **0**.

```powershell
& 'docker' 'compose' '-p' 'daa-rc-20261020-10c' 'restart' 'api'
```

35. Код завершения: **0**.

```powershell
& 'docker' 'compose' '-p' 'daa-rc-20261020-10c' 'stop'
```

36. Код завершения: **0**.

```powershell
& 'docker' 'ps' '-a' '--no-trunc' '--format' '{{json .}}'
```

37. Код завершения: **0**.

```powershell
& 'docker' 'image' 'ls' '--no-trunc' '--format' '{{.ID}} {{.Repository}}:{{.Tag}}'
```

38. Код завершения: **0**.

```powershell
& 'docker' 'volume' 'ls' '--format' '{{.Name}}'
```

39. Код завершения: **0**.

```powershell
& 'docker' 'network' 'ls' '--no-trunc' '--format' '{{.ID}} {{.Name}}'
```

## Восстановленная синтетическая копия старого layout

1. Код завершения: **0**.

```powershell
& 'docker' 'inspect' '--format' '{{.State.Running}}' 'daa-smoke-bccde37de2f8'
```

2. Код завершения: **0**.

```powershell
& 'docker' 'volume' 'create' 'daa-legacy-b48cb3f8b6bd-data'
```

3. Код завершения: **0**.

```powershell
& 'docker' 'volume' 'create' 'daa-legacy-b48cb3f8b6bd-state'
```

4. Код завершения: **0**.

```powershell
& 'docker' 'run' '--rm' '--network' 'none' '--mount' 'type=volume,source=daa-legacy-b48cb3f8b6bd-data,target=/var/lib/daa/data' 'daa-smoke-bccde37de2f8:local' 'python' '-c' 'import os;assert os.getuid()==os.getgid()==1000;assert os.stat(''/var/lib/daa/data'').st_uid==1000'
```

5. Код завершения: **0**.

```powershell
& 'docker' 'run' '--rm' '--network' 'none' '--mount' 'type=volume,source=daa-smoke-bccde37de2f8-state,target=/var/lib/daa' '--mount' 'type=volume,source=daa-smoke-bccde37de2f8-data,target=/var/lib/daa/data' '--mount' 'type=volume,source=daa-legacy-b48cb3f8b6bd-data,target=/target' 'daa-smoke-bccde37de2f8:local' 'python' '-c' '
import json, os, shutil, subprocess, sys
from pathlib import Path
import document_automation
assert os.getuid()==os.getgid()==1000
assert str(Path(document_automation.__file__)).startswith(''/usr/local/lib/'')
assert not list(Path(''/target'').iterdir())
for args in [(''backup'',''--data'',''/var/lib/daa/data'',''--output'',''/tmp/rc-backup''),
             (''verify-backup'',''--backup'',''/tmp/rc-backup''),
             (''restore'',''--backup'',''/tmp/rc-backup'',''--target'',''/tmp/rc-restored'')]:
    subprocess.run([sys.executable,''-m'',''document_automation.backup'',*args],check=True)
shutil.copytree(''/tmp/rc-restored'',''/target'',dirs_exist_ok=True)
assert Path(''/target/workflow.sqlite3'').is_file()
print(json.dumps(json.loads(Path(''/tmp/rc-backup/manifest.json'').read_text())))
'
```

6. Код завершения: **0**.

```powershell
& 'docker' 'run' '--rm' '--network' 'none' '--mount' 'type=volume,source=daa-legacy-b48cb3f8b6bd-data,target=/data,readonly' 'daa-smoke-bccde37de2f8:local' 'python' '-c' 'import os;from pathlib import Path;assert os.getuid()==os.getgid()==1000;assert Path(''/data/workflow.sqlite3'').is_file();assert not Path(''/data/data'').exists()'
```

7. Код завершения: **0**.

```powershell
& 'docker' 'run' '-d' '--name' 'daa-legacy-b48cb3f8b6bd' '--publish' '127.0.0.1::8000' '--mount' 'type=volume,source=daa-legacy-b48cb3f8b6bd-state,target=/var/lib/daa' '--mount' 'type=volume,source=daa-legacy-b48cb3f8b6bd-data,target=/var/lib/daa/data' '--env' 'DAA_MODE=demo' '--env' 'DAA_ALLOW_PAID=false' 'daa-smoke-bccde37de2f8:local'
```

8. Код завершения: **0**.

```powershell
& 'docker' 'port' 'daa-legacy-b48cb3f8b6bd' '8000/tcp'
```

9. Код завершения: **0**.

```powershell
& 'docker' 'restart' 'daa-legacy-b48cb3f8b6bd'
```

10. Код завершения: **0**.

```powershell
& 'docker' 'port' 'daa-legacy-b48cb3f8b6bd' '8000/tcp'
```

11. Код завершения: **0**.

```powershell
& 'docker' 'exec' 'daa-legacy-b48cb3f8b6bd' 'python' '-c' 'import os,sqlite3;from pathlib import Path;assert os.getuid()==os.getgid()==1000;p=Path(''/var/lib/daa'');assert len(list(p.glob(''.daa-data-*.lock'')))==1;c=sqlite3.connect(p/''data/workflow.sqlite3'');assert c.execute(''PRAGMA integrity_check'').fetchone()[0]==''ok'''
```

12. Код завершения: **0**.

```powershell
& 'docker' 'stop' 'daa-legacy-b48cb3f8b6bd'
```

## Окончательные метаданные 2.0.0rc1

1. Код завершения: **0**.

```powershell
& 'docker' 'version'
```

2. Код завершения: **0**.

```powershell
& 'docker' 'build' '--tag' 'daa-smoke-42b37a63cc5e:local' '.'
```

3. Код завершения: **0**.

```powershell
& 'docker' 'volume' 'create' 'daa-smoke-42b37a63cc5e-state'
```

4. Код завершения: **0**.

```powershell
& 'docker' 'volume' 'create' 'daa-smoke-42b37a63cc5e-data'
```

5. Код завершения: **0**.

```powershell
& 'docker' 'run' '--detach' '--name' 'daa-smoke-42b37a63cc5e' '--mount' 'type=volume,source=daa-smoke-42b37a63cc5e-state,target=/var/lib/daa' '--mount' 'type=volume,source=daa-smoke-42b37a63cc5e-data,target=/var/lib/daa/data' '--publish' '127.0.0.1::8000' '--env' 'DAA_MODE=demo' '--env' 'DAA_ALLOW_PAID=false' 'daa-smoke-42b37a63cc5e:local'
```

6. Код завершения: **0**.

```powershell
& 'docker' 'port' 'daa-smoke-42b37a63cc5e' '8000/tcp'
```

7. Код завершения: **0**.

```powershell
& 'docker' 'exec' 'daa-smoke-42b37a63cc5e' 'python' '-c' 'import os; from pathlib import Path; assert os.geteuid() == os.getegid() == 1000; p=Path(os.environ[''DAA_DATA_DIR'']); assert p.as_posix() == ''/var/lib/daa/data''; assert os.access(p, os.W_OK) and os.access(p.parent, os.W_OK); assert len(list(p.parent.glob(''.daa-data-*.lock''))) == 1'
```

8. Код завершения: **0**.

```powershell
& 'docker' 'run' '--rm' '--network' 'none' '--mount' 'type=volume,source=daa-smoke-42b37a63cc5e-state,target=/var/lib/daa' '--mount' 'type=volume,source=daa-smoke-42b37a63cc5e-data,target=/var/lib/daa/data' 'daa-smoke-42b37a63cc5e:local' 'python' '-c' 'import os
from document_automation.data_lease import DataLease, DataBusyError
assert os.geteuid() == os.getegid() == 1000
try:
    with DataLease(os.environ[''DAA_DATA_DIR''], ''maintenance''): pass
except DataBusyError:
    assert True, ''Unexpected busy lease''
else:
    assert False, ''Shared lock failed to exclude maintenance''
'
```

9. Код завершения: **0**.

```powershell
& 'docker' 'restart' 'daa-smoke-42b37a63cc5e'
```

10. Код завершения: **0**.

```powershell
& 'docker' 'port' 'daa-smoke-42b37a63cc5e' '8000/tcp'
```

11. Код завершения: **0**.

```powershell
& 'docker' 'stop' 'daa-smoke-42b37a63cc5e'
```

12. Код завершения: **0**.

```powershell
& 'docker' 'run' '--rm' '--network' 'none' '--mount' 'type=volume,source=daa-smoke-42b37a63cc5e-state,target=/var/lib/daa' '--mount' 'type=volume,source=daa-smoke-42b37a63cc5e-data,target=/var/lib/daa/data' 'daa-smoke-42b37a63cc5e:local' 'python' '-c' 'import os
from document_automation.data_lease import DataLease, DataBusyError
assert os.geteuid() == os.getegid() == 1000
try:
    with DataLease(os.environ[''DAA_DATA_DIR''], ''maintenance''): pass
except DataBusyError:
    assert False, ''Unexpected busy lease''
else:
    assert True, ''Shared lock failed to exclude maintenance''
'
```

13. Код завершения: **0**.

```powershell
& 'docker' 'run' '--rm' '--network' 'none' 'daa-smoke-42b37a63cc5e:local' 'python' '-c' 'import os,importlib.metadata as m;from document_automation.api import create_app;assert os.getuid()==os.getgid()==1000;assert m.version(''document-automation-assistant'')==''2.0.0rc1'';assert create_app().version==''2.0.0rc1'';print(''release_metadata_verified'')'
```

## HTTP smoke Compose

Переменные: DAA_API_PORT=64065, DAA_UI_PORT=64066, DAA_MODE=demo, DAA_ALLOW_PAID=false.
DAA_API_URL=http://127.0.0.1:64065; DAA_UI_URL=http://127.0.0.1:64066.
`python -B scripts/smoke_demo.py` — exit 0; A/B/C/V: оба DOCX HTTP 200, T: оба HTTP 409;
FastAPI /health и Streamlit /_stcore/health: HTTP 200.
