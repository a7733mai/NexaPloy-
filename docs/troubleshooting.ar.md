# حل المشكلات

## أين تبحث

```bash
nexaploy ping                                   # agent API up? which version?
nexaploy jobs --status failure                  # failed jobs on this server
nexaploy jobs <job id>                          # every step with its output and the error
journalctl -u nexaploy-agent-worker -n 200 -f   # jobs, heartbeat, control plane polling
journalctl -u nexaploy-agent-api -n 200         # local API
docker ps --format '{{.Names}}\t{{.Status}}'    # nexaploy-db, nexaploy-traefik, <bench>-*
docker logs nexaploy-traefik --tail 100         # TLS / routing
docker logs nexaploy-db --tail 100              # MariaDB
cat /var/log/nexaploy-install.log               # servers set up by cloud-init or the server image
```

في لوحة التحكم تعرض كل **Agent Job** الخطوات والمخرجات نفسها. وتعرض صفحة الخادم آخر نبضة
والمعلومات (القرص والذاكرة والحاويات غير السليمة) وآخر المهام.

## المهام الفاشلة

تُنفَّذ كل مهمة على خطوات، ويمكن تنفيذ كل خطوة مرتين بأمان. عندما تفشل خطوة تتوقف المهمة وتُتخطى
الخطوات التالية. تخبرك مخرجات الخطوة الفاشلة بالسبب.

- **المهام المحلية** (التي بدأها سطر الأوامر `nexaploy` أو الواجهة المحلية): أصلح السبب ثم نفّذ
  `nexaploy retry <job id> --wait`. تُتخطى الخطوات التي نجحت، وتكمل المهمة من الخطوة الفاشلة.
  المهمة التي حُذفت أسرارها (مثل كلمة مرور المسؤول) لا يمكن إعادة محاولتها، فأرسلها من جديد.
- **مهام لوحة التحكم** تُعاد من لوحة التحكم: يستدعي مسؤول المنصة الدالة `retry` لـ Agent Job.
  ينشئ ذلك مهمة جديدة بالمعاملات نفسها. يكتمل نقل الموقع الفاشل عند إعادة محاولة مهمته الفاشلة.
- إذا أُعيد تشغيل الوكيل أثناء مهمة، يعيدها إلى قائمة الانتظار عند بدئه، وتُتخطى الخطوات التي نجحت.

## شهادات TLS (Let's Encrypt)

يحصل Traefik على شهادة لكل موقع عبر تحدي ACME على HTTP. إذا ظهر تحذير شهادة أو الشهادة
الافتراضية لـ Traefik:

- يجب أن يشير DNS الموقع (سجل A/AAAA) إلى **هذا** الخادم. بعد النقل إلى خادم آخر حدّث كل نطاقات الموقع.
- **يجب أن يكون المنفذ 80 متاحًا من الإنترنت.** تتحقق Let's Encrypt عبر المنفذ 80، لذا فإن جدار
  حماية أو مجموعة أمان تسمح بالمنفذ 443 فقط يمنع إصدار الشهادة.
- حدّد بريدًا حقيقيًا عبر `--acme-email` (شغّل `install.sh` مجددًا لتغييره). القيمة الافتراضية
  `admin@example.com` مرفوضة.
- ابحث عن أخطاء `acme` في `docker logs nexaploy-traefik`. تحدّ Let's Encrypt من عدد الشهادات التي
  تصدرها، فبعد عدة محاولات فاشلة انتظر قبل المحاولة مجددًا.
- تُحفظ الشهادات في `/opt/nexaploy/proxy/data/certificates/acme.json`، ويجب أن تكون صلاحياته 600
  ومالكه المعرّف 3100.
- الاختبارات المحلية بلا DNS عام لا تحصل على شهادات. استخدم `curl -k --resolve` كما في `e2e/README.md`.

## المنفذان 80/443 مستخدمان

يتوقف `install.sh` برسالة `port 80 is already in use` عندما يستمع خادم ويب آخر (nginx أو Apache
أو Caddy أو Traefik آخر). أوقفه أو استخدم خادمًا جديدًا. لا مشكلة إن كان الوكيل العكسي لـ Nexaploy
نفسه يستخدم المنفذ (عند إعادة التشغيل). يتخطى `--skip-preflight` هذا الفحص، لكن الوكيل العكسي
سيفشل في البدء على منفذ مستخدم.

## MariaDB 11.8 وTLS (Frappe v16)

عميل MariaDB في صور Frappe v16 **يتطلب TLS** للاتصال بقاعدة البيانات. لذلك القاعدة المشتركة هي
`mariadb:11.8`، التي تدعم TLS جاهزًا. من أعراض الخادم الأقدم (مثل 10.6) أن مواقع v16 تفشل في
الاتصال، أو أن `bench new-site` يفشل بأخطاء TLS/SSL.

- شغّل `install.sh` مجددًا: ملف قاعدة البيانات يستخدم `mariadb:11.8`. يُرقّى مجلد بيانات 10.6
  الموجود في مكانه، وتبقى مواقع v15 تعمل.
- إذا غيّرت الصورة عبر `MARIADB_IMAGE` في `/opt/nexaploy/db/.env` فاستخدم 11.4 أو أحدث.
- على v16 يستخدم `bench new-site` الخيار `--mariadb-user-host-login-scope=%`. يرجع الوكيل إلى
  `--no-mariadb-socket` فقط مع الإصدارات الأقدم (v14) التي لا تعرف الخيار الجديد.

## امتلاء القرص

- ترسل لوحة التحكم بريدًا إلى الفريق عندما يتجاوز استخدام القرص **85%**، مرة كل 6 ساعات على
  الأكثر لكل خادم.
- اعثر على ما يشغل المساحة: `df -h /opt/nexaploy` و`du -sh /opt/nexaploy/*` و`docker system df`.
- الأسباب الشائعة هي صور البنش القديمة (`docker image prune` يحذف الصور التي لا تستخدمها أي حاوية)،
  والنسخ المحلية في `/opt/nexaploy/backups` (خفّض `retention_days` في `agent.toml` واضبط التخزين
  البعيد)، ومخرجات البناء في `/opt/nexaploy/images/builds`.
- تتوقف MariaDB عندما يمتلئ قرصها. حرّر بعض المساحة ثم راجع `docker logs nexaploy-db` و`docker ps`
  (تُعاد الحاوية تلقائيًا).

## الخادم يظهر غير متصل (Offline)

يصبح الخادم *Offline* إذا لم تصل منه نبضة لمدة 5 دقائق.

- `systemctl status nexaploy-agent-worker`، فالعامل هو من يرسل النبضة.
- يُظهر `journalctl -u nexaploy-agent-worker` الرسالة `authentication failed (401)` عندما يكون رمز
  الخادم خاطئًا، مثلًا بعد أرشفة الخادم أو تدوير رمزه. سجّل مجددًا برمز تسجيل جديد مع `--force-register`.
- يحتاج الخادم إلى اتصال HTTPS صادر إلى لوحة التحكم. لا حاجة أبدًا لاتصالات واردة.

## فشل ترقية الوكيل

راجع [ترقية الوكيل](operating.md#agent-upgrades). تُظهر مخرجات المهمة الخطوة الفاشلة. إذا لم يبدأ
الوكيل الجديد، يسترجع المراقب الإصدار السابق بعد نحو 3 دقائق. يدويًا: `sudo nexaploy-agent rollback`.

## تعذّر سحب صورة البنش على خادم آخر

بدون سجل صور (registry) توجد الصورة فقط على الخادم الذي بناها. اضبط **Nexaploy Settings → Registry**
لتُرفع الصور المبنية وتسحبها الخوادم الأخرى.
