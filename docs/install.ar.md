# مرجع التثبيت

يجهّز `install.sh` (الموجود في المستودع في `agent/install/install.sh`) الخادم ويثبّت الوكيل.
شغّله بصلاحيات root. يمكنك تشغيله مجددًا في أي وقت: التشغيل الثاني يرقّي الوكيل ويحافظ على
الإعدادات والأسرار والشهادات الحالية.

```bash
curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
  | sudo bash -s -- [options]
```

## الخيارات

| الخيار | المعنى |
|---|---|
| `--control-plane URL` | العنوان الأساسي للوحة التحكم، مثل `https://cp.example.com`. اتركه لخادم مستقل. |
| `--token TOKEN` | مع `--control-plane`: **رمز التسجيل** الذي يُستخدم مرة واحدة. بدونه: رمز الوصول لواجهة الوكيل المحلية (عشوائي افتراضيًا). |
| `--server-id ID` | معرّف الخادم (`^[a-z0-9][a-z0-9-]{1,62}$`). افتراضيًا تعيّنه لوحة التحكم أو يُولَّد. يُستخدم للخوادم المستضافة المنشأة يدويًا: `--server-id <id> --token <server token>`. |
| `--acme-email EMAIL` | بريد حساب Let's Encrypt الذي يستخدمه الوكيل العكسي. حدّده، فالقيمة الافتراضية `admin@example.com` تُظهر تحذيرًا. |
| `--agent-source SRC` | رابط git أو مسار محلي لمستودع Nexaploy (أو لمجلد `agent/`). الافتراضي: المستودع الرسمي على GitHub. |
| `--branch BRANCH` | فرع أو وسم git لـ `--agent-source` (الافتراضي `main`). استخدم وسم إصدار مثل `v0.1.0` في الإنتاج. |
| `--frappe-docker-ref REF` | مرجع git لـ `frappe_docker` المستخدم سياقًا لبناء الصور (الافتراضي `main`). |
| `--force-register` | سجّل مجددًا حتى لو كان رمز الخادم محفوظًا. |
| `--skip-preflight` | تخطّ فحوص المعمارية والذاكرة والقرص والمنافذ. |
| `--root DIR` | مجلد التثبيت (الافتراضي `/opt/nexaploy`). المسارات الأخرى مخصصة للاختبار. |
| `--skip-packages` | لا تثبّت حزم النظام. يجب أن تكون Docker وإضافة compose وPython ≥ 3.11 وrclone وgit وjq مثبتة مسبقًا. |
| `--no-systemd` | لا تثبّت وحدات systemd. عندها تشغّل `nexaploy-agent api` و`nexaploy-agent worker` بنفسك. |
| `--skip-frappe-docker` | لا تنسخ `frappe_docker`. لن يستطيع هذا الخادم بناء الصور. |
| `--with-control-plane DOMAIN` | **جديد.** الكل في واحد: يثبّت لوحة التحكم أيضًا على هذا الخادم للنطاق `DOMAIN`. |
| `-h`, `--help` | اعرض المساعدة. |

## ماذا يفعل

1. **الفحص المسبق.** يتحقق من Ubuntu 22.04/24.04 (وDebian 12 بأفضل جهد) ومعمارية x86_64 أو aarch64،
   وذاكرة 2 جيجابايت على الأقل، و20 جيجابايت حرة على الأقل في مجلد التثبيت (في التثبيت الأول فقط)،
   وأن المنفذين 80/443 غير مستخدمين. لا مشكلة إن كان الوكيل العكسي لـ Nexaploy نفسه يستخدمهما.
2. **الحزم.** يثبّت Docker CE مع إضافة compose (من مستودع Docker)، وgit وjq وacl وrclone وopenssl،
   وPython ≥ 3.11 مع venv.
3. **المستخدمون والمجلدات.** ينشئ مستخدمي نظام بمعرّفات ثابتة: 3100 لـ Traefik و3101 لـ MariaDB
   و3102 لـ Redis و3113 لـ Frappe. ينشئ شجرة `/opt/nexaploy` بالمالكين المناسبين.
4. **الشبكات.** ينشئ شبكتي Docker `proxy-network` و`nexaploy-db`.
5. **الوكيل.** ينسخ المصدر إلى `/opt/nexaploy/agent/src` ويضمّن قوالب البنش، ثم يثبّت الوكيل في
   `/opt/nexaploy/agent/.venv`. يربط `nexaploy` و`nexaploy-agent` في `/usr/local/bin`. ينسخ
   `image-builder/` إلى `/opt/nexaploy/images/` وينسخ مستودع `frappe_docker`.
6. **الإعدادات.** يكتب `/opt/nexaploy/config/agent.toml` (بصلاحيات 600 مع الإبقاء على القيم الحالية)
   و`db.env` (كلمة مرور root عشوائية لـ MariaDB) و`rclone.conf` فارغًا. مع `--control-plane`
   و`--token` يسجّل الخادم، ثم ترسل لوحة التحكم إعدادات تخزين النسخ الاحتياطية.
7. **الخدمات.** يثبّت `nexaploy-agent-api` و`nexaploy-agent-worker` ويشغّلهما، ثم يشغّل قاعدة
   MariaDB المشتركة (`nexaploy-db`) والوكيل العكسي (`nexaploy-proxy`).
8. **الملخص.** يطبع الإصدار ونتيجة `nexaploy ping`.

## الملفات على الخادم

```
/opt/nexaploy/
├── agent/            src/ (source), .venv/ (agent), data/jobs.db (job queue)
├── config/           agent.toml, db.env, rclone.conf      (mode 600)
├── proxy/            Traefik: docker-compose.yml, config/sites/<site>.yml, data/certificates/acme.json
├── db/               MariaDB 11.8: docker-compose.yml, data/
├── images/           build.sh, Containerfile, frappe_docker/, builds/<tag>/
├── benches/<bench>/  docker-compose.yml, .env, sites/, logs/
└── backups/          staging area before upload
```

## الخدمات وسطر الأوامر

| الوحدة | الأمر | الغرض |
|---|---|---|
| `nexaploy-agent-api` | `nexaploy-agent api` | واجهة REST محلية على `127.0.0.1:8787` (رمز الوصول من `agent.toml`) |
| `nexaploy-agent-worker` | `nexaploy-agent worker` | ينفّذ المهام واحدة تلو الأخرى، ويرسل النبضة ويطلب المهام من لوحة التحكم |

```bash
nexaploy ping                       # is the agent API up? (prints the version)
nexaploy jobs [ID] [--wait]         # list jobs / follow one
nexaploy retry <job id> --wait      # retry a failed local job from the failed step
nexaploy bench list | site list
nexaploy-agent version | upgrade --version X | rollback
```

## صور الخوادم

يبني `packer/` لقطات Ubuntu 24.04 لـ Hetzner Cloud وDigitalOcean. تأتي جاهزة بـ Docker والوكيل
والصور الأساسية مسحوبة مسبقًا. عند أول إقلاع يكتمل التثبيت من بيانات المستخدم (user-data):

| بيانات المستخدم | النتيجة |
|---|---|
| `NEXAPLOY_CONTROL_PLANE=…` و`NEXAPLOY_TOKEN=…` | ينضم الخادم إلى تلك اللوحة |
| `NEXAPLOY_DOMAIN=…` | الكل في واحد (جديد) |
| `NEXAPLOY_ACME_EMAIL=…` | بريد Let's Encrypt (اختياري) |

بدون بيانات مستخدم، شغّل لاحقًا `sudo nexaploy-firstboot --control-plane URL --token T`.
التفاصيل في `packer/README.md`.

## الإزالة

```bash
sudo systemctl disable --now nexaploy-agent-api nexaploy-agent-worker
```

تبقى البنشات والمواقع وMariaDB والوكيل العكسي تعمل، فهي مشاريع Docker Compose عادية في
`/opt/nexaploy`. يمكنك إدارتها بـ `docker compose` بعد ذلك.
