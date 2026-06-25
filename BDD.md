# BDD — Multi-Environment Tool Plugin

Поведенческая спецификация плагина `multienv` для Hermes Agent.
Не жёсткий Given-When-Then, а кейсы, ожидания, edge cases и критерии приёмки.

---

## 1. env_connect — создание подключения

### 1.1. Базовые кейсы

**SSH подключение с явным slug**
- Агент вызывает `env_connect(slug="serverA", type="ssh", host="a.example.com", user="deploy", key_path="~/.ssh/id_rsa")`
- Ожидание: возвращается `{"slug": "serverA", "status": "connected", "type": "ssh", "cwd": "~"}`
- Внутренне: создан `SSHEnvironment(host, user, port=22, key_path, cwd="~", timeout=180)`, вызван `init_session()`, `FileSyncManager.sync(force=True)`
- Подключение сохранено в `EnvironmentRegistry` под slug `serverA`
- Последующий `env_list` показывает `serverA` с `status=connected`

**Docker подключение с auto-generated slug**
- Агент вызывает `env_connect(type="docker", image="node:22")` без slug
- Ожидание: возвращается `{"slug": "env-1", "status": "connected", "type": "docker", ...}`
- Slug сгенерирован автоматически (инкрементальный: `env-1`, `env-2`, ...)


**Подключение с кастомным timeout**
- `env_connect(type="ssh", host="...", user="...", timeout=60)`
- Timeout передаётся в backend constructor
- Команды на этом env завершаются по истечении 60с

### 1.2. Кейсы с портами и нестандартными параметрами

**SSH с нестандартным портом**
- `env_connect(type="ssh", host="...", user="...", port=2222, key_path="...")`
- `SSHEnvironment` создан с `port=2222`
- SSH-команды идут на порт 2222

**Docker с volumes и mount**
- `env_connect(type="docker", image="python:3.12", volumes=["/host/dir:/container/dir"], auto_mount_cwd=true, cwd="/host/dir")`
- Host-директория смонтирована в контейнер
- Файлы в `/host/dir` видны в контейнере как `/container/dir`

### 1.3. Edge cases и ошибки

**Повторный connect с тем же slug**
- `env_connect(slug="serverA", ...)` когда `serverA` уже существует
- Ожидание: возвращается ошибка `{"error": "Environment 'serverA' already connected. Use env_disconnect first or choose a different slug."}`
- Существующее подключение НЕ затронуто

**SSH без host**
- `env_connect(type="ssh", user="deploy")` без host
- Ожидание: `{"error": "SSH connection requires 'host' and 'user' parameters"}`
- `SSHEnvironment.__init__` не вызван

**SSH без user**
- `env_connect(type="ssh", host="a.example.com")` без user
- Ожидание: та же ошибка — required params missing

**SSH с недоступным хостом**
- `env_connect(type="ssh", host="nonexistent.example.com", user="x", key_path="x")`
- `_establish_connection()` падает по timeout (15с)
- Ожидание: `{"error": "SSH connection failed: ..."}`
- Registry не содержит slug

**SSH с неверным ключом**
- `env_connect(type="ssh", host="...", user="...", key_path="/nonexistent/key")`
- SSH-клиент падает с ошибкой авторизации
- Ожидание: `{"error": "SSH connection failed: Permission denied (publickey)."}`
- Registry не содержит slug

**Docker без установленного Docker**
- `env_connect(type="docker", image="node:22")` на машине без Docker
- Ожидание: `{"error": "Docker is not available ..."}`
- Аналогично core `DockerEnvironment` — проверка через `_docker_executable`

**Неизвестный type**
- `env_connect(type="kubernetes")`
- Ожидание: `{"error": "Unknown environment type: 'kubernetes'. Supported: docker, ssh"}`

### 1.4. Критерии приёмки (env_connect)

- [ ] `env_connect` с корректными параметрами создаёт env и возвращает slug
- [ ] `init_session()` вызывается после конструирования
- [ ] Slug уникален в рамках registry — повторное использование отклоняется
- [ ] Автогенерация slug работает (env-1, env-2, ...)
- [ ] Все required-параметры проверяются до создания env-инстанса
- [ ] Ошибки соединения не оставляют partial state в registry
- [ ] Timeout соединения не превышает разумного предела (15с для SSH)

---

## 2. env_list — список подключений

### 2.1. Базовые кейсы

**Пустой список**
- Агент вызывает `env_list()` до любых `env_connect`
- Ожидание: `{"environments": []}`

**Один env**
- После `env_connect(slug="serverA", type="ssh", ...)`
- `env_list()` → `{"environments": [{"slug": "serverA", "type": "ssh", "status": "connected", "cwd": "~", "connected_at": "..."}]}`

**Несколько envs**
- После connect serverA, serverB, containerC
- `env_list()` → массив из 3 элементов, отсортированный по `connected_at` или slug

### 2.2. Edge cases

**Env после disconnect**
- `env_disconnect("serverA")` → `env_list()` не содержит serverA

**Множественные connect/disconnect в сессии**
- connect serverA → connect serverB → disconnect serverA → connect serverC
- `env_list()` → [serverB, serverC] (serverA отсутствует)

### 2.3. Критерии приёмки (env_list)

- [ ] Возвращает все активные подключения
- [ ] Отключённые подключения не появляются
- [ ] Метаданные включают: slug, type, status, cwd, connected_at
- [ ] Вызов без аргументов

---

## 3. env_disconnect — закрытие подключения

### 3.1. Базовые кейсы

**Disconnect SSH**
- `env_disconnect("serverA")` где serverA — SSH
- Ожидание: вызван `SSHEnvironment.cleanup()` → `sync_back()` + закрытие ControlMaster socket
- Возвращается `{"slug": "serverA", "status": "disconnected"}`
- `env_list()` больше не содержит serverA

**Disconnect Docker**
- `env_disconnect("containerC")` где containerC — Docker
- Ожидание: вызван `DockerEnvironment.cleanup()` → `docker stop` + `docker rm -f` (если не persistent-across-processes)
- Registry очищен


### 3.2. Edge cases

**Disconnect несуществующего slug**
- `env_disconnect("nonexistent")`
- Ожидание: `{"error": "Environment 'nonexistent' not found"}`

**Повторный disconnect**
- `env_disconnect("serverA")` после первого disconnect
- Ожидание: `{"error": "Environment 'serverA' not found"}`

**Disconnect во время активной команды**
- `env_disconnect("serverA")` пока `env_tool("serverA", "terminal", {command: "sleep 300"})` выполняется
- Ожидание: env.cleanup() пытается убить процесс, но активный tool-call может вернуть partial output или ошибку
- Registry очищен, но агент может получить ошибку от незавершённого tool-call

### 3.3. Критерии приёмки (env_disconnect)

- [ ] `cleanup()` вызывается на env-инстансе
- [ ] SSH: `sync_back()` выполняется (изменения `.hermes/` тащатся обратно)
- [ ] Registry очищен после disconnect
- [ ] Несуществующий slug → ошибка, не crash
- [ ] Идемпотентность — повторный disconnect = ошибка "not found", не exception

---

## 4. env_tool — выполнение операций

### 4.1. terminal

**Команда на SSH env**
- `env_tool("serverA", "terminal", {command: "ls -la"})` где serverA — SSH
- Ожидание: `SSHEnvironment.execute("ls -la", cwd="~")` → `{"output": "...", "returncode": 0}`
- Результат: `{"output": "drwxr-xr-x ...", "exit_code": 0}`

**Команда на Docker env**
- `env_tool("containerC", "terminal", {command: "node --version"})` где containerC — Docker
- Ожидание: `DockerEnvironment.execute("node --version")` → выполняется в контейнере


**CWD tracking через команды**
- `env_tool("serverA", "terminal", {command: "cd /var/log"})`
- `env_tool("serverA", "terminal", {command: "ls"})`
- Ожидание: вторая команда выполняется в `/var/log` (BaseEnvironment CWD tracking через stdout markers)
- `env_list()` показывает `cwd: "/var/log"` для serverA

**Команда с явным cwd**
- `env_tool("serverA", "terminal", {command: "ls", args: {workdir: "/tmp"}})` (если args поддерживает workdir)
- Ожидание: команда выполняется в `/tmp`, но env.cwd не меняется (или меняется — зависит от BaseEnvironment.execute)

**Команда с timeout**
- `env_tool("serverA", "terminal", {command: "sleep 300", args: {timeout: 5}})`
- Ожидание: команда прерывается через 5с, возвращается `{"output": "... [Command timed out after 5s]", "exit_code": 124}`

**Длинный вывод**
- `env_tool("serverA", "terminal", {command: "find / -name '*.log'"})` — вывод > 50KB
- Ожидание: вывод обрезан (truncation), возвращается head + tail + omitted count

**Non-zero exit code**
- `env_tool("serverA", "terminal", {command: "exit 42"})`
- Ожидание: `{"output": "", "exit_code": 42}` — не считается ошибкой плагина

### 4.2. read_file

**Чтение файла на SSH**
- `env_tool("serverA", "read_file", {path: "/etc/hosts"})`
- Ожидание: `ShellFileOperations(serverA_env).read_file("/etc/hosts")` → выполняет `cat /etc/hosts` на remote
- Возвращает `{"content": "127.0.0.1 localhost\n...", "total_lines": 5}`

**Чтение с offset и limit**
- `env_tool("serverA", "read_file", {path: "/var/log/syslog", offset: 100, limit: 50})`
- Ожидание: читаются строки 100-149

**Чтение несуществующего файла**
- `env_tool("serverA", "read_file", {path: "/nonexistent"})`
- Ожидание: `{"error": "File not found: /nonexistent"}` или эквивалент

**Чтение бинарного файла**
- `env_tool("serverA", "read_file", {path: "/usr/bin/ls"})`
- Ожидание: вывод с заменой non-UTF8 на U+FFFD (как `errors="replace"` в BaseEnvironment)

### 4.3. write_file

**Запись файла на SSH**
- `env_tool("serverA", "write_file", {path: "/tmp/test.txt", content: "hello world"})`
- Ожидание: `ShellFileOperations.write_file("/tmp/test.txt", "hello world")` → `cat <<EOF > /tmp/test.txt` на remote
- Возвращает `{"status": "ok"}`

**Запись поверх существующего файла**
- `env_tool("serverA", "write_file", {path: "/tmp/test.txt", content: "new content"})`
- Ожидание: файл перезаписан (overwrite, не append)

**Запись в несуществующую директорию**
- `env_tool("serverA", "write_file", {path: "/tmp/newdir/test.txt", content: "x"})`
- Ожидание: директория создана (`mkdir -p`), файл записан

**Запись большого файла**
- `env_tool("serverA", "write_file", {path: "/tmp/big.txt", content: "x" * 100000})`
- Ожидание: файл записан через stdin pipe (не через ARG_MAX-limited command)

### 4.4. patch

**Replace mode на SSH**
- `env_tool("serverA", "patch", {path: "/tmp/test.txt", old_string: "hello", new_string: "bye"})`
- Ожидание: `ShellFileOperations.patch_replace(...)` → `sed` на remote
- Возвращает `{"status": "ok", "replacements": 1}`

**Replace all**
- `env_tool("serverA", "patch", {path: "/tmp/test.txt", old_string: "x", new_string: "y", replace_all: true})`
- Ожидание: все вхождения заменены

**V4A multi-file patch**
- `env_tool("serverA", "patch", {mode: "patch", patch: "..."})`
- Ожидание: `ShellFileOperations.patch_v4a(...)` → multi-file patch на remote

**Old string не найден**
- `env_tool("serverA", "patch", {path: "/tmp/test.txt", old_string: "nonexistent", new_string: "x"})`
- Ожидание: `{"error": "old_string not found in file"}` или эквивалент

### 4.5. search_files

**Search content на SSH**
- `env_tool("serverA", "search_files", {pattern: "ERROR", path: "/var/log", target: "content"})`
- Ожидание: `ShellFileOperations.search("ERROR", path="/var/log", target="content")` → `grep` на remote
- Возвращает `{"matches": [...]}`

**Search files by name**
- `env_tool("serverA", "search_files", {pattern: "*.py", target: "files", path: "/home/deploy"})`
- Ожидание: `find /home/deploy -name '*.py'` на remote

**Search без результатов**
- `env_tool("serverA", "search_files", {pattern: "ZZZNONEXISTENTZZZ", path: "/tmp"})`
- Ожидание: `{"matches": []}` — не ошибка

### 4.6. execute_code

#### MVP (Path A — plain Python)

**Простой Python на SSH**
- `env_tool("serverA", "execute_code", {code: "print(1+1)"})`
- Ожидание: script.py записан на remote через base64, `env.execute("python3 /tmp/hermes_exec_XXX/script.py")`
- Возвращается `{"status": "success", "output": "2\n", "tool_calls_made": 0, "duration_seconds": 0.5}`

**Python с ошибкой**
- `env_tool("serverA", "execute_code", {code: "raise ValueError('test')"})`
- Ожидание: `{"status": "error", "output": "Traceback...", "error": "Script exited with code 1"}`

**Python без python3 на remote**
- `env_tool("serverA", "execute_code", {code: "print(1)"})` где на serverA нет python3
- Ожидание: `{"status": "error", "error": "Python 3 is not available in the ssh terminal environment..."}`

**Python с long output**
- `env_tool("serverA", "execute_code", {code: "print('x' * 100000)"})`
- Ожидание: output обрезан до MAX_STDOUT_BYTES (50KB) — head + tail + omitted

#### Full parity (Path C — RPC poll loop)

**Python с tool calls на SSH**
- `env_tool("serverA", "execute_code", {code: "import hermes_tools\nresult = hermes_tools.terminal('ls -la')\nprint(result)"})`
- Ожидание:
  - `hermes_tools.py` (file-based transport) сгенерирован и ship-нут на remote
  - Script запущен на remote: `env.execute("python3 script.py")`
  - Script вызывает `hermes_tools.terminal('ls -la')` → пишет `req_000001` файл на remote
  - Plugin poll loop читает `req_000001` через `env.execute("cat req_000001")`
  - Plugin dispatch: `env.execute("ls -la")` на serverA
  - Plugin пишет `res_000001` через `env.execute("echo | base64 -d > res_000001")`
  - Script читает `res_000001`, продолжает, печатает result
  - Plugin возвращает `{"status": "success", "output": "...", "tool_calls_made": 1, ...}`

**Python с read_file call на SSH**
- `env_tool("serverA", "execute_code", {code: "import hermes_tools\nr = hermes_tools.read_file('/etc/hosts')\nprint(r)"})`
- Ожидание: `file_ops.read_file("/etc/hosts")` на serverA → возвращает content
- `tool_calls_made: 1`

**Python с web_search call (core dispatcher)**
- `env_tool("serverA", "execute_code", {code: "import hermes_tools\nr = hermes_tools.web_search('test')\nprint(r)"})`
- Ожидание: web_search routed через `handle_function_call("web_search", args, task_id)` — core dispatcher
- Web_search делает HTTP call с host-процесса, не с serverA
- `tool_calls_made: 1`

**Python с multiple tool calls**
- `env_tool("serverA", "execute_code", {code: "import hermes_tools\nhermes_tools.terminal('mkdir -p /tmp/test')\nhermes_tools.write_file('/tmp/test/a.txt', 'hello')\nr = hermes_tools.read_file('/tmp/test/a.txt')\nprint(r)"})`
- Ожидание: 3 tool calls, все на serverA
- `tool_calls_made: 3`, `status: "success"`

**Python с tool call limit**
- `env_tool("serverA", "execute_code", {code: "...50+ tool calls..."})`
- Ожидание: после max_tool_calls (default 50) — `{"error": "Tool call limit reached (50)"}` в response file
- Script получает error от `hermes_tools.terminal(...)`, может обработать

**Python с timeout**
- `env_tool("serverA", "execute_code", {code: "import time; time.sleep(300)"})`
- Ожидание: `{"status": "timeout", "error": "Script timed out after 300s and was killed."}`

**Python interrupted**
- Пользователь отправляет новое сообщение во время execute_code
- Ожидание: `{"status": "interrupted", "output": "... [execution interrupted]"}`

**Cleanup после execute_code**
- После завершения (success/error/timeout) — `env.execute("rm -rf /tmp/hermes_exec_XXX")` на remote
- Sandbox dir не остаётся на remote

**Concurrent execute_code на разных envs**
- `env_tool("serverA", "execute_code", {code: "..."})` и `env_tool("serverB", "execute_code", {code: "..."})` одновременно
- Ожидание: каждый имеет свой sandbox_id, свой rpc_dir, свой poll loop thread
- Результаты не смешиваются

### 4.7. Edge cases (env_tool общие)

**Несуществующий slug**
- `env_tool("nonexistent", "terminal", {command: "ls"})`
- Ожидание: `{"error": "Environment 'nonexistent' not found. Use env_list to see available environments."}`

**Несуществующий tool_name**
- `env_tool("serverA", "nonexistent_tool", {})`
- Ожидание: `{"error": "Unknown tool_name: 'nonexistent_tool'. Supported: terminal, read_file, write_file, patch, search_files, execute_code"}`

**Несуществующий slug + несуществующий tool**
- `env_tool("ghost", "ghost_tool", {})`
- Ожидание: ошибка про slug (priority — slug check first)

**Args не соответствуют tool**
- `env_tool("serverA", "terminal", {path: "/etc/hosts"})` — terminal не принимает path
- Ожидание: `terminal` handler игнорирует неизвестные args или возвращает `{"error": "Missing required parameter: command"}`

**Disconnected env mid-operation**
- `env_disconnect("serverA")` вызван пока `env_tool("serverA", "terminal", ...)` выполняется
- Ожидание: tool-call возвращает ошибку или partial output
- Registry больше не содержит serverA

### 4.8. Критерии приёмки (env_tool)

- [ ] Все 6 tool_names работают: terminal, read_file, write_file, patch, search_files, execute_code
- [ ] Tool calls идут на указанный env, НЕ на DEFAULT env
- [ ] Несуществующий slug → ошибка, не crash
- [ ] Несуществующий tool_name → ошибка со списком supported
- [ ] CWD tracking работает across multiple env_tool calls на одном env
- [ ] Timeout команд соблюдается
- [ ] Long output обрезается (truncation)
- [ ] ANSI escape sequences strip-ятся в output
- [ ] Secrets redact-ятся в output
- [ ] execute_code Path A: plain Python работает
- [ ] execute_code Path C: tool-RPC работает, tool calls идут на правильный env
- [ ] execute_code cleanup: sandbox dir удаляется на remote
- [ ] Concurrent execute_code на разных envs не смешиваются

---

## 5. Мульти-environment сценарии

### 5.1. Одновременная работа

**SSH + Docker в одном turn (local через core tools)**
- `env_connect(slug="serverA", type="ssh", ...)` + `env_connect(slug="containerC", type="docker", ...)`
- `terminal({command: "hostname"})` (core) → local hostname
- `env_tool("serverA", "terminal", {command: "hostname"})` → serverA hostname
- `env_tool("containerC", "terminal", {command: "hostname"})` → container hostname
- Все 3 результата разные, все в одном turn. Local — через core tools (TERMINAL_ENV=local), remote — через plugin.

**Переключение без reconnect**
- `env_tool("serverA", "terminal", {command: "ls"})`
- `env_tool("serverB", "terminal", {command: "ls"})`
- `env_tool("serverA", "terminal", {command: "pwd"})` — serverA connection still alive
- Ожидание: третье обращение к serverA использует существующее соединение (ControlMaster reuse), не новый connect

**CWD изолирован per env**
- `env_tool("serverA", "terminal", {command: "cd /var/log"})`
- `env_tool("serverB", "terminal", {command: "cd /tmp"})`
- `env_tool("serverA", "terminal", {command: "pwd"})` → `/var/log` (serverA CWD не затронут serverB)
- `env_tool("serverB", "terminal", {command: "pwd"})` → `/tmp`

### 5.2. Файл операции across envs

**Копирование файла между envs (через агент)**
- `env_tool("serverA", "read_file", {path: "/etc/hosts"})` → content
- `env_tool("serverB", "write_file", {path: "/tmp/hosts_copy", content: "<content from serverA>"})`
- Ожидание: файл на serverB содержит content из serverA
- Агент выступает как посредник — plugin не делает cross-env copy напрямую

### 5.3. Критерии приёмки (мульти-env)

- [ ] N envs работают одновременно без interference
- [ ] CWD изолирован per env
- [ ] Connections переиспользуются (no reconnect per call)
- [ ] Cross-env операции работают через агент-посредник

---

## 6. Изоляция от core

### 6.1. Core tools не затронуты

**Default terminal работает параллельно**
- `TERMINAL_ENV=local` (default)
- `env_connect(slug="serverA", type="ssh", ...)`
- `terminal({command: "whoami"})` (core) → выполняется на local (DEFAULT env)
- `env_tool("serverA", "terminal", {command: "whoami"})` (plugin) → выполняется на serverA
- Оба результата корректны, не смешиваются

**Default execute_code работает параллельно**
- `execute_code({code: "print('hello')"})` (core) → использует `_active_environments["default"]` (local)
- `env_tool("serverA", "execute_code", {code: "print('hello')"})` (plugin) → использует plugin registry
- Результаты не interfere

**Core _active_environments не мутируется**
- После любых env_connect / env_tool / env_disconnect
- `_active_environments` core содержит те же entries что и до plugin операций
- Проверка: `len(_active_environments)` не изменился

### 6.2. Критерии приёмки (изоляция)

- [ ] Core `terminal` работает независимо от plugin
- [ ] Core `execute_code` работает независимо от plugin
- [ ] Core `_active_environments` не мутируется plugin-ом
- [ ] Plugin toolset `multienv` включается/отключается через `hermes tools` без влияния на core tools
- [ ] Prompt caching не нарушается — plugin tools регистрируются at startup, не mid-conversation

---

## 7. Cleanup и lifecycle

### 7.1. Session end

**Все envs cleaned up при session end**
- connect serverA, serverB, containerC
- Сессия завершается (on_session_end hook)
- Ожидение: `cleanup()` вызван для каждого env
- SSH: sync_back + ControlMaster close
- Docker: container stop/remove

**Session end без активных envs**
- on_session_end hook вызван, registry пуст
- Ожидание: no-op, no error

### 7.2. Process termination

**SIGKILL / crash**
- Процесс Hermes убит (SIGKILL)
- Ожидание: `BaseEnvironment.__del__` вызван для каждого env (best-effort, не guaranteed при SIGKILL)
- SSH: ControlMaster socket остаётся в `/tmp/hermes-ssh/` (orphan, но ControlPersist=300 истечёт)
- Docker: container остаётся running (orphan, будет убит Docker daemon или при следующем старте)

### 7.3. Критерии приёмки (cleanup)

- [ ] on_session_end cleanup-ит все envs
- [ ] env_disconnect cleanup-ит конкретный env
- [ ] `__del__` fallback работает при нормальном exit
- [ ] SSH ControlMaster socket закрывается при cleanup
- [ ] Docker container останавливается при cleanup (если не persistent-across-processes)

---

## 8. Sync стратегия

### 8.1. SSH с sync_policy=minimal_hermes (default)

**Credentials доступны на remote**
- `env_connect(type="ssh", host="...", sync_policy="minimal_hermes")`
- `env_tool("serverA", "terminal", {command: "gh auth status"})`
- Ожидание: GitHub token из `~/.hermes/credentials/` sync-нут на remote → `gh` аутентифицирован

**Skills доступны на remote**
- Plugin skill на хосте: `~/.hermes/skills/foo/scripts/bar.py`
- `env_tool("serverA", "terminal", {command: "python3 ~/.hermes/skills/foo/scripts/bar.py"})`
- Ожидание: skill файл sync-нут, скрипт выполняется

**sync_back при disconnect**
- На remote: `hermes_tools.write_file("~/.hermes/skills/new_skill.py", "...")` через execute_code
- `env_disconnect("serverA")`
- Ожидание: `sync_back()` тащит `new_skill.py` обратно на хост
- Файл доступен на хосте после disconnect

### 8.2. SSH с sync_policy=none

**Credentials НЕ sync-нуты**
- `env_connect(type="ssh", host="untrusted.example.com", sync_policy="none")`
- `env_tool("untrusted", "terminal", {command: "ls ~/.hermes/credentials/"})`
- Ожидание: директория пуста или не существует — credentials не отправлены на untrusted host

### 8.3. Docker (bind-mount — sync не нужен)

**Файлы видны напрямую**
- `env_connect(type="docker", volumes=["~/.hermes:/root/.hermes"])`
- `env_tool("containerC", "terminal", {command: "ls /root/.hermes/credentials/"})`
- Ожидание: credentials видны через bind-mount, sync не требуется

### 8.4. Критерии приёмки (sync)

- [ ] sync_policy=minimal_hermes: credentials + skills + cache sync-утся на SSH env
- [ ] sync_policy=none: ничего не sync-ётся
- [ ] sync_back: изменения `.hermes/` на remote тащатся обратно при disconnect
- [ ] Docker: bind-mount заменяет sync
- [ ] User project files НЕ sync-утся — агент работает с ними через shell на remote

---

## 9. Inventory (preconfigured envs)

### 9.1. config.yaml inventory

**Preconfigured envs из config**
- `config.yaml` содержит:
  ```yaml
  multienv:
    environments:
      serverA:
        type: ssh
        host: a.example.com
        user: deploy
        key_path: ~/.ssh/id_rsa
      containerC:
        type: docker
        image: node:22
  ```
- Plugin загружает inventory при `register(ctx)`
- `env_list()` показывает preconfigured envs со `status=available` (не `connected`)
- `env_tool("serverA", "terminal", {command: "ls"})` — auto-connect при первом обращении
- Возвращается результат как если бы был явный `env_connect`

**Override preconfigured через env_connect**
- `env_connect(slug="serverA", type="ssh", host="different.example.com", ...)` — переопределяет inventory
- Ожидание: используется new connection, не inventory config

### 9.2. Критерии приёмки (inventory)

- [ ] Preconfigured envs видны в env_list до connect
- [ ] Auto-connect при первом env_tool обращении к preconfigured env
- [ ] env_connect может override preconfigured env
- [ ] Inventory читается из `multienv.environments` в config.yaml

---

## 10. Tool schema и footprint

### 10.1. Schema стабильна

**3 tools независимо от числа envs**
- 0 envs connected → 3 tools в schema
- 5 envs connected → 3 tools в schema
- 20 envs connected → 3 tools в schema

**Schema не меняется mid-conversation**
- Plugin регистрирует tools при `register(ctx)` (startup)
- Tools не добавляются/удаляются mid-conversation
- Prompt caching не нарушается

### 10.2. Критерии приёмки (schema)

- [ ] Ровно 3 (или 4 с env_disconnect) tools в schema
- [ ] Schema не растёт с числом envs
- [ ] Schema не меняется mid-conversation
- [ ] Plugin toolset `multienv` появляется в `hermes tools` UI

---

## 11. Security

### 11.1. Secret redaction

**Secrets не утекают в output**
- `env_tool("serverA", "terminal", {command: "env"})` — env vars могут содержать secrets
- Ожидание: output проходит через `redact_sensitive_text()` — secrets заменены на `[REDACTED]`

### 11.2. Sync на untrusted hosts

**sync_policy=none для untrusted**
- `env_connect(type="ssh", host="untrusted.example.com", sync_policy="none")`
- Ожидание: credentials/skills/cache НЕ отправлены
- Агент может выполнять shell-команды, но без Hermes credentials

### 11.3. execute_code sandbox isolation

**Sandbox изолирован**
- `execute_code` script не имеет доступа к host-процессу (кроме через RPC)
- Script не может читать файлы за пределами env FS
- RPC allow-list: только 7 SANDBOX_ALLOWED_TOOLS

### 11.4. Критерии приёмки (security)

- [ ] Secret redaction работает на env_tool output
- [ ] sync_policy=none блокирует credential sync
- [ ] execute_code sandbox ограничен 7 tool calls
- [ ] Plugin не передаёт host secrets в args/schema

---

## 12. Error handling и resilience

### 12.1. Network errors

**SSH connection dropped mid-session**
- `env_tool("serverA", "terminal", {command: "ls"})` — SSH connection разорвана
- Ожидание: `SSHEnvironment.execute()` падает с SSH error
- Plugin возвращает `{"error": "SSH connection lost: ..."}`
- env остаётся в registry (возможно с status=disconnected)
- Повторный `env_tool("serverA", ...)` → попытка reconnect через ControlMaster (если socket жив) или ошибка

**Docker container stopped externally**
- `docker stop` выполнен вручную пока plugin env активен
- `env_tool("containerC", "terminal", {command: "ls"})` — container не running
- Ожидание: `DockerEnvironment.execute()` падает → `{"error": "..."}`
- `env_disconnect("containerC")` — cleanup пытается stop/rm (no-op если уже stopped)

### 12.2. Timeout handling

**Command timeout на SSH**
- `env_tool("serverA", "terminal", {command: "sleep 1000"})` с timeout=5
- Ожидание: команда убита через 5с, `{"output": "... [Command timed out after 5s]", "exit_code": 124}`

**execute_code timeout**
- `env_tool("serverA", "execute_code", {code: "import time; time.sleep(1000)"})` с timeout=30
- Ожидание: script убит через 30с, poll loop остановлен, sandbox dir cleanup, `{"status": "timeout", ...}`

### 12.3. Критерии приёмки (error handling)

- [ ] Network errors возвращают JSON error, не crash процесс
- [ ] Timeout команды возвращают exit_code=124
- [ ] execute_code timeout cleanup-ит sandbox
- [ ] Ошибки одного env не влияют на другие envs
- [ ] Ошибки plugin tools не влияют на core tools

---

## 13. Интеграция с Hermes

### 13.1. Plugin registration

**Plugin discovered и loaded**
- `~/.hermes/plugins/multienv/plugin.yaml` существует
- `~/.hermes/plugins/multienv/__init__.py` содержит `register(ctx)`
- Plugin discovery находит plugin при старте Hermes
- 3 (или 4) tools зарегистрированы в registry под toolset `multienv`

**Plugin в `hermes tools` UI**
- `hermes tools` показывает toolset `multienv`
- Toggle включения/выключения работает
- Когда disabled — tools не появляются в schema

### 13.2. Lifecycle hooks

**on_session_end cleanup**
- Plugin регистрирует `on_session_end` hook
- При завершении сессии hook вызывает cleanup для всех envs в registry

### 13.3. Критерии приёмки (интеграция)

- [ ] Plugin обнаруживается Hermes plugin discovery
- [ ] Tools появляются под toolset `multienv`
- [ ] `hermes tools` UI показывает toolset
- [ ] on_session_end hook cleanup-ит envs
- [ ] Plugin не модифицирует core файлы
- [ ] Plugin импортирует backend classes (SSHEnvironment, etc.) — не дублирует

---

## 14. Модель-ориентированные кейсы

### 14.1. Модель понимает env_tool

**Модель выбирает правильный env**
- System prompt содержит описание plugin tools
- Модель видит: `env_tool(env_slug, tool_name, args)`
- Модель вызывает `env_tool("serverA", "terminal", {command: "ls"})` — понимает что нужно указать slug
- Модель не путает env_tool с core terminal

**Модель использует env_list для discovery**
- Модель не знает какие envs подключены
- Вызывает `env_list()` → получает список
- Затем `env_tool("serverA", ...)` с правильным slug

**Модель выводит args структуру из core schemas**
- Модель видит core `read_file` schema (path, offset, limit)
- Для `env_tool("serverA", "read_file", {path: "/etc/hosts"})` — модель вкладывает args правильно
- Модель не кладёт path на верхний уровень вместо args

### 14.2. Критерии приёмки (model)

- [ ] Модель корректно использует env_tool с slug + tool_name + args
- [ ] Модель вызывает env_list для discovery
- [ ] Модель вкладывает tool-specific params в args, не на верхний уровень
- [ ] Модель не путает env_tool с core tools

---

## 15. Non-goals (что плагин НЕ делает)

- **Не заменяет core tools** — `terminal`, `read_file`, etc. продолжают работать через `TERMINAL_ENV`
- **Не мутирует `_active_environments`** — plugin имеет свой registry
- **Не модифицирует core файлы** — plugin-only
- **Не sync-ает user project files** — только `~/.hermes/` internals (credentials, skills, cache)
- **Не поддерживает background processes на remote** (MVP) — core `process_registry` tightly coupled с `_active_environments` и core `terminal_tool` task_id системой. Plugin variant = reimplement process_registry (~500 строк: output buffering, crash recovery, poll/wait/kill API). Future: plugin-side `BackgroundProcessRegistry` per env.
- **Не поддерживает Modal/Daytona/Singularity** (MVP) — только docker и ssh
- **Не поддерживает `type=local`** — local покрывается core Hermes (`TERMINAL_ENV=local`). Plugin только для remote environments (ssh, docker).
- **Не делает cross-env copy** — агент является посредником (read from A → write to B)
- **Не persist-ит registry между сессиями** — envs не переживают restart Hermes (MVP)
