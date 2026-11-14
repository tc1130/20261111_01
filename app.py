# -*- coding: utf-8 -*-
"""
在线收作业网站后端
技术栈：Flask + Flask-Login + Flask-SQLAlchemy + SQLite
"""
import os
import re
import shutil
import zipfile
from datetime import datetime
from io import BytesIO

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, abort, jsonify
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user, UserMixin
)
from flask_sqlalchemy import SQLAlchemy

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_ROOT = os.path.join(BASE_DIR, "uploads")

app = Flask(__name__)
app.config["SECRET_KEY"] = "homework-collection-secret-key-2026"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "homework.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 单个文件最大 100MB

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "请先登录"


# -------------------- 数据模型 --------------------
class User(db.Model, UserMixin):
    """学生 / 学委账号"""
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(30), unique=True, nullable=False, index=True)  # 学号
    name = db.Column(db.String(50), nullable=False)                                # 姓名
    password = db.Column(db.String(200), nullable=False)                           # 明文密码（便于学委查询找回）
    is_monitor = db.Column(db.Boolean, default=False)                              # 是否学委
    created_at = db.Column(db.DateTime, default=datetime.now)

    def set_password(self, raw):
        self.password = raw

    def check_password(self, raw):
        return self.password == raw


class Task(db.Model):
    """作业任务"""
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)                              # 任务名
    deadline = db.Column(db.DateTime, nullable=False)                              # 截止时间
    formats = db.Column(db.String(200), default="pdf")                             # 允许的文档格式（逗号分隔扩展名）
    is_deleted = db.Column(db.Boolean, default=False)                              # 软删除
    created_at = db.Column(db.DateTime, default=datetime.now)


class TaskRecipient(db.Model):
    """任务的发送对象（哪些学生需要提交）"""
    __tablename__ = "task_recipients"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    task = db.relationship("Task", backref="recipients")
    user = db.relationship("User", backref="task_links")


class Submission(db.Model):
    """提交记录"""
    __tablename__ = "submissions"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    filename = db.Column(db.String(300), nullable=False)                           # 服务器保存的文件名
    original_name = db.Column(db.String(300))                                      # 原始文件名
    submitted_at = db.Column(db.DateTime, default=datetime.now)

    task = db.relationship("Task", backref="submissions")
    user = db.relationship("User", backref="submissions")


class Notification(db.Model):
    """学生端通知"""
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    content = db.Column(db.String(500), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    user = db.relationship("User", backref="notifications")


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# -------------------- 工具函数 --------------------
def init_monitor_account():
    """初始化学委账号（若不存在则创建）"""
    monitor = User.query.filter_by(student_id="2024652063").first()
    if not monitor:
        monitor = User(
            student_id="2024652063",
            name="唐晨",
            is_monitor=True,
        )
        monitor.set_password("8888888888Tc.")
        db.session.add(monitor)
        db.session.commit()


def safe_filename(text):
    """清理文件名中的非法字符（保留中文）"""
    return re.sub(r'[\\/:*?"<>|\r\n]', "_", text)


def task_upload_dir(task_id):
    """某个任务的存储目录"""
    d = os.path.join(UPLOAD_ROOT, str(task_id))
    os.makedirs(d, exist_ok=True)
    return d


def allowed_extension(filename, task):
    """校验扩展名是否在任务允许的格式内"""
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    allowed = [f.strip().lower().lstrip(".") for f in (task.formats or "pdf").split(",")]
    return ext in allowed


def build_saved_filename(task, user, ext):
    """按 {学号}-{姓名}-{任务名} 规则命名"""
    return f"{user.student_id}-{user.name}-{safe_filename(task.title)}{ext}"


def is_deadline_passed(task):
    return datetime.now() > task.deadline


# -------------------- 统一登录 / 注册 --------------------
@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    if request.method == "POST":
        student_id = request.form.get("student_id", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(student_id=student_id).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("home"))
        flash("学号或密码错误", "danger")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    if request.method == "POST":
        student_id = request.form.get("student_id", "").strip()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not student_id or not name or not password:
            flash("请填写完整信息", "danger")
            return redirect(url_for("login", show_register=1))
        elif password != confirm:
            flash("两次输入的密码不一致", "danger")
            return redirect(url_for("login", show_register=1))
        elif User.query.filter_by(student_id=student_id).first():
            flash("该学号已注册", "danger")
            return redirect(url_for("login", show_register=1))
        else:
            user = User(student_id=student_id, name=name, is_monitor=False)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("注册成功，请登录", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/home")
@login_required
def home():
    """按角色跳转到对应端"""
    if current_user.is_monitor:
        return redirect(url_for("monitor_dashboard"))
    return redirect(url_for("student_dashboard"))


# -------------------- 学生端 --------------------
@app.route("/student")
@login_required
def student_dashboard():
    if current_user.is_monitor:
        return redirect(url_for("monitor_dashboard"))

    # 我可见的任务（未删除，且我是发送对象）
    visible_task_ids = [
        r.task_id for r in TaskRecipient.query.filter_by(user_id=current_user.id).all()
    ]
    tasks = Task.query.filter(
        Task.id.in_(visible_task_ids),
        Task.is_deleted == False,  # noqa: E712
    ).order_by(Task.created_at.desc()).all()  # 新发布的任务排在最上面

    # 我的提交（task_id -> submission）
    my_subs = {
        s.task_id: s for s in Submission.query.filter_by(user_id=current_user.id).all()
    }

    # 我的提交记录（提交记录弹窗用）
    subs = Submission.query.filter_by(user_id=current_user.id) \
        .order_by(Submission.submitted_at.desc()).all()

    notifications = Notification.query.filter_by(user_id=current_user.id) \
        .order_by(Notification.created_at.desc()).limit(20).all()

    return render_template(
        "student/dashboard.html",
        tasks=tasks,
        my_subs=my_subs,
        subs=subs,
        notifications=notifications,
        now=datetime.now(),
    )


@app.route("/student/check_new_msg")
@login_required
def student_check_new_msg():
    """学生端轮询：检测是否有新消息（学委发布/删除任务产生），有则弹窗提示并标记已提示"""
    if current_user.is_monitor:
        return jsonify({"has_new": False})
    unread = Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).all()
    if unread:
        for n in unread:
            n.is_read = True
        db.session.commit()
        return jsonify({"has_new": True})
    return jsonify({"has_new": False})


@app.route("/student/task/<int:task_id>/submit", methods=["GET", "POST"])
@login_required
def student_submit(task_id):
    if current_user.is_monitor:
        return redirect(url_for("monitor_dashboard"))

    task = db.session.get(Task, task_id)
    if not task or task.is_deleted:
        abort(404)

    # 是否是指定提交对象
    recipient = TaskRecipient.query.filter_by(
        task_id=task_id, user_id=current_user.id
    ).first()
    if not recipient:
        abort(403)

    # 是否 AJAX 请求（学生端弹窗内提交）
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if is_deadline_passed(task):
        if is_ajax:
            return jsonify({"ok": False, "msg": "已过截止时间，无法提交"})
        flash("已过截止时间，无法提交", "danger")
        return redirect(url_for("student_dashboard"))

    existing = Submission.query.filter_by(task_id=task_id, user_id=current_user.id).first()

    if request.method == "POST":
        file = request.files.get("file")
        if not file or file.filename == "":
            msg = "请选择要提交的文件"
        elif not allowed_extension(file.filename, task):
            msg = f"文件格式不符合要求，允许格式：{task.formats}"
        else:
            ext = os.path.splitext(file.filename)[1].lower()
            filename = build_saved_filename(task, current_user, ext)
            save_path = os.path.join(task_upload_dir(task_id), filename)

            # 若已提交过，先删除旧文件
            if existing:
                old_path = os.path.join(task_upload_dir(task_id), existing.filename)
                if os.path.exists(old_path):
                    os.remove(old_path)

            file.save(save_path)

            if existing:
                existing.filename = filename
                existing.original_name = file.filename
                existing.submitted_at = datetime.now()
            else:
                sub = Submission(
                    task_id=task_id,
                    user_id=current_user.id,
                    filename=filename,
                    original_name=file.filename,
                )
                db.session.add(sub)
            db.session.commit()
            if is_ajax:
                return jsonify({"ok": True, "msg": "提交成功"})
            flash("提交成功", "success")
            return redirect(url_for("student_records"))

        if is_ajax:
            return jsonify({"ok": False, "msg": msg})
        flash(msg, "danger")

    return render_template("student/submit.html", task=task, existing=existing)


@app.route("/student/records")
@login_required
def student_records():
    if current_user.is_monitor:
        return redirect(url_for("monitor_dashboard"))
    subs = Submission.query.filter_by(user_id=current_user.id) \
        .order_by(Submission.submitted_at.desc()).all()
    return render_template("student/records.html", subs=subs)


@app.route("/notification/<int:nid>/delete", methods=["POST"])
@login_required
def delete_notification(nid):
    """删除单条通知"""
    n = db.session.get(Notification, nid)
    if n and n.user_id == current_user.id:
        db.session.delete(n)
        db.session.commit()
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 404


@app.route("/notifications/delete", methods=["POST"])
@login_required
def delete_notifications():
    """批量删除通知（接收 JSON 的 ids 列表）"""
    ids = []
    if request.is_json:
        ids = request.get_json().get("ids", [])
    if ids:
        Notification.query.filter(
            Notification.user_id == current_user.id,
            Notification.id.in_(ids),
        ).delete(synchronize_session=False)
        db.session.commit()
    return jsonify({"ok": True})


# -------------------- 学委端 --------------------
@app.route("/monitor")
@login_required
def monitor_dashboard():
    if not current_user.is_monitor:
        return redirect(url_for("student_dashboard"))
    students = User.query.filter_by(is_monitor=False).order_by(User.student_id.asc()).all()
    tasks = Task.query.filter_by(is_deleted=False).order_by(Task.created_at.desc()).all()
    return render_template("monitor/dashboard.html", students=students, tasks=tasks)


@app.route("/monitor/tasks")
@login_required
def monitor_tasks():
    if not current_user.is_monitor:
        return redirect(url_for("student_dashboard"))
    tasks = Task.query.filter_by(is_deleted=False).order_by(Task.created_at.desc()).all()
    return render_template("monitor/tasks.html", tasks=tasks)


@app.route("/monitor/task/create", methods=["GET", "POST"])
@login_required
def monitor_create_task():
    if not current_user.is_monitor:
        return redirect(url_for("student_dashboard"))

    students = User.query.filter_by(is_monitor=False).order_by(User.student_id.asc()).all()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        deadline_str = request.form.get("deadline", "").strip()
        formats = request.form.getlist("formats")
        recipient_ids = request.form.getlist("recipients")

        if not title:
            flash("请填写任务名", "danger")
        elif not deadline_str:
            flash("请设置截止时间", "danger")
        elif not formats:
            flash("请至少选择一种提交文档格式", "danger")
        elif not recipient_ids:
            flash("请至少勾选一个发送对象", "danger")
        else:
            try:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M")
            except ValueError:
                deadline = None
            if not deadline:
                flash("截止时间格式错误", "danger")
            else:
                task = Task(
                    title=title,
                    deadline=deadline,
                    formats=",".join(f.strip().lower().lstrip(".") for f in formats),
                )
                db.session.add(task)
                db.session.flush()  # 获取 task.id

                for uid in recipient_ids:
                    db.session.add(TaskRecipient(task_id=task.id, user_id=int(uid)))
                    # 给每个目标学生发通知
                    u = db.session.get(User, int(uid))
                    if u:
                        db.session.add(Notification(
                            user_id=u.id,
                            content=f"新任务《{title}》已发布，截止时间 {deadline.strftime('%Y-%m-%d %H:%M')}，请及时提交。",
                        ))
                db.session.commit()
                flash("任务发布成功", "success")
                return redirect(url_for("monitor_dashboard"))

    return render_template("monitor/create_task.html", students=students)


@app.route("/monitor/task/<int:task_id>/delete", methods=["POST"])
@login_required
def monitor_delete_task(task_id):
    if not current_user.is_monitor:
        return redirect(url_for("student_dashboard"))
    task = db.session.get(Task, task_id)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if task:
        task.is_deleted = True
        # 通知所有发送对象：任务已删除
        for r in task.recipients:
            db.session.add(Notification(
                user_id=r.user_id,
                content=f"任务《{task.title}》已被删除。",
            ))
        db.session.commit()
        if is_ajax:
            return jsonify({"ok": True, "msg": "任务已删除"})
        flash("任务已删除", "success")
    return redirect(url_for("monitor_dashboard"))


@app.route("/monitor/task/<int:task_id>")
@login_required
def monitor_task_detail(task_id):
    if not current_user.is_monitor:
        return redirect(url_for("student_dashboard"))
    task = db.session.get(Task, task_id)
    if not task:
        abort(404)

    recipients = task.recipients
    total = len(recipients)
    submitted = Submission.query.filter_by(task_id=task_id).all()
    submitted_user_ids = {s.user_id for s in submitted}
    submitted_count = len(submitted_user_ids)
    unsubmitted_count = total - submitted_count

    # 未提交学生名单
    unsubmitted = [r.user for r in recipients if r.user_id not in submitted_user_ids]
    unsubmitted.sort(key=lambda u: u.student_id)

    template = "monitor/task_detail_fragment.html" if request.args.get("fragment") == "1" else "monitor/task_detail.html"
    return render_template(
        template,
        task=task,
        total=total,
        submitted_count=submitted_count,
        unsubmitted_count=unsubmitted_count,
        unsubmitted=unsubmitted,
        submissions=submitted,
    )


@app.route("/monitor/task/<int:task_id>/download")
@login_required
def monitor_task_download(task_id):
    if not current_user.is_monitor:
        return redirect(url_for("student_dashboard"))
    task = db.session.get(Task, task_id)
    if not task:
        abort(404)

    dir_path = task_upload_dir(task_id)
    memory = BytesIO()
    with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(dir_path):
            for fname in os.listdir(dir_path):
                full = os.path.join(dir_path, fname)
                if os.path.isfile(full):
                    zf.write(full, arcname=fname)
    memory.seek(0)
    zip_name = safe_filename(task.title) + ".zip"
    return send_file(
        memory,
        as_attachment=True,
        download_name=zip_name,
        mimetype="application/zip",
    )


@app.route("/monitor/students")
@login_required
def monitor_students():
    if not current_user.is_monitor:
        return redirect(url_for("student_dashboard"))
    keyword = request.args.get("q", "").strip()
    query = User.query.filter_by(is_monitor=False)
    if keyword:
        query = query.filter(
            db.or_(
                User.student_id.contains(keyword),
                User.name.contains(keyword),
            )
        )
    students = query.order_by(User.student_id.asc()).all()
    return render_template("monitor/students.html", students=students, keyword=keyword)


@app.route("/monitor/student/<int:user_id>")
@login_required
def monitor_student_detail(user_id):
    if not current_user.is_monitor:
        return redirect(url_for("student_dashboard"))
    student = db.session.get(User, user_id)
    if not student or student.is_monitor:
        abort(404)

    # 提交记录
    subs = Submission.query.filter_by(user_id=user_id) \
        .order_by(Submission.submitted_at.desc()).all()

    # 未按时提交的任务（指定给该学生、已过截止、且未提交）
    links = TaskRecipient.query.filter_by(user_id=user_id).all()
    overdue_tasks = []
    for link in links:
        task = link.task
        if task.is_deleted:
            continue
        if is_deadline_passed(task):
            sub = Submission.query.filter_by(task_id=task.id, user_id=user_id).first()
            if not sub:
                overdue_tasks.append(task)

    template = "monitor/student_detail_fragment.html" if request.args.get("fragment") == "1" else "monitor/student_detail.html"
    return render_template(
        template,
        student=student,
        subs=subs,
        overdue_tasks=overdue_tasks,
    )


@app.route("/monitor/student/<int:user_id>/delete", methods=["POST"])
@login_required
def monitor_delete_student(user_id):
    if not current_user.is_monitor:
        return redirect(url_for("student_dashboard"))
    student = db.session.get(User, user_id)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if student and not student.is_monitor:
        # 删除该学生提交的所有文件
        for sub in student.submissions:
            path = os.path.join(task_upload_dir(sub.task_id), sub.filename)
            if os.path.exists(path):
                os.remove(path)
        # 删除提交记录、任务关联、通知
        Submission.query.filter_by(user_id=user_id).delete()
        TaskRecipient.query.filter_by(user_id=user_id).delete()
        Notification.query.filter_by(user_id=user_id).delete()
        db.session.delete(student)
        db.session.commit()
        if is_ajax:
            return jsonify({"ok": True, "msg": "学生已删除"})
        flash("学生已删除", "success")
    return redirect(url_for("monitor_dashboard"))


@app.route("/monitor/submission/<int:sub_id>/delete", methods=["POST"])
@login_required
def monitor_delete_submission(sub_id):
    if not current_user.is_monitor:
        return redirect(url_for("student_dashboard"))
    sub = db.session.get(Submission, sub_id)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if sub:
        path = os.path.join(task_upload_dir(sub.task_id), sub.filename)
        if os.path.exists(path):
            os.remove(path)
        back = request.form.get("back")
        db.session.delete(sub)
        db.session.commit()
        if is_ajax:
            return jsonify({"ok": True, "msg": "提交文件已删除"})
        flash("提交文件已删除", "success")
        if back == "student":
            return redirect(url_for("monitor_student_detail", user_id=sub.user_id))
        return redirect(url_for("monitor_task_detail", task_id=sub.task_id))
    return redirect(url_for("monitor_dashboard"))


# -------------------- 启动 --------------------
# 在模块加载时初始化数据库（无论是 python app.py 还是 gunicorn 启动都会执行）
with app.app_context():
    db.create_all()
    init_monitor_account()

if __name__ == "__main__":
    # debug 由环境变量 FLASK_DEBUG 控制，默认关闭（生产环境安全）
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
