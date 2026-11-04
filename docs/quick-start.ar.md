# البدء السريع

هناك طريقتان لتشغيل Nexaploy:

- **الكل في واحد (all-in-one).** تعمل لوحة التحكم ومواقعك على خادم واحد. تناسب هذه الطريقة
  شركة واحدة أو وكالة صغيرة.
- **لوحة تحكم مستضافة مع خوادم BYOS.** تدير لوحة تحكم واحدة خوادم كثيرة. بعضها تنشئه المنصة
  على Hetzner Cloud أو DigitalOcean، وبعضها خوادم يحضرها عملاؤك ويسجلونها برمز يُستخدم مرة واحدة.

## المتطلبات (لكل خادم)

- خادم جديد بنظام **Ubuntu 22.04 أو 24.04** ومعمارية x86_64 أو arm64. يعمل Debian 12 بأفضل جهد.
- **ذاكرة 2 جيجابايت** على الأقل و**20 جيجابايت مساحة حرة**. يستهلك كل بنش Frappe نحو
  1–2 جيجابايت من الذاكرة، لذا خطط لـ 4 جيجابايت أو أكثر للاستخدام الفعلي.
- **المنفذان 80 و443 غير مستخدمين** ومتاحان من الإنترنت. تتحقق Let's Encrypt من الشهادات
  عبر المنفذ 80.
- صلاحيات root، واتصال HTTPS صادر إلى Docker Hub وGitHub ولوحة التحكم.

## الخيار أ — كل شيء على خادم واحد (جديد) { #all-in-one }

!!! note "جديد"
    الخيار `--with-control-plane` قيد الإضافة إلى `install.sh` الآن. إذا لم يظهر في
    `--help` لديك، فاستخدم الخيار ب وثبّت لوحة التحكم على الخادم نفسه.

1. وجّه سجل **A** في DNS لنطاق لوحة التحكم (مثل `cloud.example.com`) إلى عنوان IP العام للخادم.
2. شغّل المثبّت بصلاحيات root:

    ```bash
    curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
      | sudo bash -s -- --with-control-plane cloud.example.com --acme-email ops@example.com
    ```

3. افتح `https://cloud.example.com` وسجّل الدخول، ثم تابع من [أول موقع](#first-site) أدناه.

يمكنك أيضًا البدء من **صورة خادم Nexaploy** (لقطة Hetzner Cloud أو DigitalOcean تُبنى من `packer/`).
ضع `NEXAPLOY_DOMAIN=cloud.example.com` (واختياريًا `NEXAPLOY_ACME_EMAIL=...`) في بيانات
المستخدم (user-data) للخادم، فيكتمل التثبيت تلقائيًا عند أول إقلاع.

## الخيار ب — لوحة تحكم مستضافة مع خوادم BYOS { #byos }

### 1. تثبيت لوحة التحكم

لوحة التحكم تطبيق Frappe (الإصدار v15 أو v16). ثبّته على بنش:

```bash
git clone https://github.com/a7733mai/NexaPloy- /srv/nexaploy
cd ~/frappe-bench
bench get-app /srv/nexaploy/control-plane
bench --site cp.example.com install-app nexaploy
bench --site cp.example.com migrate
bench --site cp.example.com enable-scheduler
```

ينشر كل إصدار أيضًا صورة لوحة التحكم `ghcr.io/a7733mai/nexaploy-control-plane:<version>`.
يتبع الوسمان `:16` و`:latest` أحدث إصدار على Frappe v16.

ينشئ تثبيت التطبيق دور **Nexaploy Customer** وثلاث خطط افتراضية: خادم BYOS بـ 10$ شهريًا،
وخادم مستضاف بـ 40$ شهريًا، وموقع بـ 5$ شهريًا.

### 2. نشر `install.sh`

تنزّل الخوادم المثبّت من `https://<control-plane>/install.sh`. قدّم الملف
`agent/install/install.sh` على هذا المسار من خادم الويب الذي أمام Frappe. مثلًا في nginx:

```nginx
location = /install.sh { alias /srv/nexaploy/agent/install/install.sh; default_type text/x-shellscript; }
```

يُرفق `install.sh` أيضًا بكل إصدار على GitHub.

### 3. إعداد المنصة

في واجهة المكتب (desk) افتح **Nexaploy Settings**:

- **النطاق الجذر (Root domain)**: تُسمّى المواقع الجديدة `<name>.<root domain>`. وجّه سجل DNS
  عامًا (`*.<root domain>`) إلى خوادمك المستضافة.
- **تخزين النسخ الاحتياطية** (نقطة وصول متوافقة مع S3 ومنطقة ودلو ومفاتيح). عند الحفظ يستلم
  كل خادم إعدادات التخزين (المهمة `server.configure_backups`). بدونها تبقى النسخ على الخادم.
- **سجل الصور (Registry)** (اختياري، مثل `ghcr.io/your-org`): عندها يمكن للخوادم الأخرى سحب
  صورة البنش المبنية على خادم واحد.
- **بريد التنبيهات** لتنبيهات التشغيل.

للخوادم المستضافة، أضف **Cloud Provider** (رمز API لـ Hetzner Cloud أو DigitalOcean)، ثم
زامن مناطقه وأحجامه وفعّل ما تريد بيعه.

### 4. إضافة خادم

=== "BYOS (خادم العميل)"

    في لوحة التحكم افتح **Servers → Add your own server → Generate install command**. شغّل الأمر
    الظاهر بصلاحيات root على الخادم الجديد:

    ```bash
    curl -fsSL https://cp.example.com/install.sh | sudo bash -s -- \
      --token <registration-token> --control-plane https://cp.example.com
    ```

    يُستخدم الرمز مرة واحدة وصلاحيته 24 ساعة. تعرض الصفحة الخادم **نشطًا** عند وصول أول نبضة منه.

=== "مستضاف (تنشئه المنصة)"

    في لوحة التحكم افتح **Servers → Create hosted server**، ثم اختر المزوّد والمنطقة والحجم.
    تنشئ Nexaploy الخادم الافتراضي، ويشغّل cloud-init الأمر `install.sh` برمز تسجيل. يستغرق
    ذلك عادة 5 إلى 10 دقائق.

=== "صورة الخادم"

    أنشئ خادمًا من لقطة Nexaploy واجعل بيانات المستخدم (user-data):

    ```
    NEXAPLOY_CONTROL_PLANE=https://cp.example.com
    NEXAPLOY_TOKEN=<registration-token>
    ```

## أول موقع { #first-site }

1. **Bench Groups → New Bench Group.** اختر إصدار Frappe (الافتراضي v16) والتطبيقات من السوق
   أو من تطبيقاتك على GitHub. في صفحة المجموعة اضغط **Deploy now**، فيبني خادم البناء صورة
   Docker واحدة (المهمة `image.build`). يستغرق البناء الأول بعض الوقت.
2. في الصفحة نفسها اختر خادمًا تحت **Benches on your servers** واضغط **Deploy to server**
   (المهمة `bench.create`).
3. **Sites → New Site.** اختر مكان النشر وأدخل اسم الموقع واختر التطبيقات، ثم اضغط
   **Create site**. احفظ كلمة مرور المسؤول، فهي تُعرض مرة واحدة فقط.
4. يُقدَّم الموقع عبر HTTPS فور إصدار Let's Encrypt لشهادته. لاستخدام نطاقك الخاص، وجّه
   DNS الخاص به إلى الخادم ثم أضف النطاق من صفحة الموقع.

### بدون لوحة تحكم (مستقل)

يعمل الوكيل وحده أيضًا من سطر الأوامر:

```bash
curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
  | sudo bash -s -- --acme-email ops@example.com
nexaploy build --tag nexaploy/bench:erp-v16-1 --major 16 --apps-json apps.json --wait
nexaploy bench create erp-v16 --image nexaploy/bench:erp-v16-1 --wait
nexaploy site create erp.example.com --bench erp-v16 -a erpnext --wait   # asks for the admin password
```
