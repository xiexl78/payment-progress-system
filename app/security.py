# -*- coding: utf-8 -*-
"""安全基线：CSRF 保护（批次 4a，自建实现，不引入新依赖）

- 服务端在 session 中维护随机 token，模板通过 ``csrf_token()`` 输出隐藏字段；
- 所有非安全方法（POST/PUT/PATCH/DELETE）校验表单字段 ``csrf_token``
  或请求头 ``X-CSRF-Token``，失败返回 403；
- 显式配置注入且使用默认 SECRET_KEY 的测试模式默认关闭（``CSRF_ENABLED=False``），
  测试可通过 ``CSRF_ENABLED=True`` 打开校验。
"""

import hmac
import secrets

from flask import abort, request, session

CSRF_SESSION_KEY = '_csrf_token'
CSRF_FORM_FIELD = 'csrf_token'
CSRF_HEADER = 'X-CSRF-Token'
_SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS', 'TRACE')


def generate_csrf_token():
    """取当前会话 CSRF token，没有则生成（模板里调用）。"""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def rotate_csrf_token():
    """敏感操作后轮换 CSRF token（旧页面表单 token 失效，Cookie 重新签名）。"""
    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = token
    return token


def _submitted_token():
    return (request.form.get(CSRF_FORM_FIELD)
            or request.headers.get(CSRF_HEADER) or '')


def _csrf_failed():
    """CSRF 校验失败：写审计（容错）后返回 403 中文页。"""
    try:
        from .audit import record
        from .models import db
        record('auth.csrf_blocked', object_type='endpoint', result='denied',
               critical=False,
               after={'endpoint': request.endpoint, 'path': request.path,
                      'method': request.method})
        db.session.commit()
    except Exception:
        pass
    abort(403, description='CSRF 校验失败，请刷新页面后重新提交')


def init_security(app):
    """注册 CSRF 校验、模板 helper（create_app 调用一次）。"""

    @app.context_processor
    def _inject_csrf():  # noqa: ANN001
        return {'csrf_token': generate_csrf_token}

    @app.before_request
    def _drop_legacy_node_tokens():  # noqa: ANN001
        # CR-05 需求⑤：CR-04 曾把节点操作令牌写进 Cookie 会话（旧会话可能已
        # 膨胀到浏览器上限），首次请求就丢弃，Cookie 立即回落到正常体积。
        if app.config.get('NODE_TOKENS_SESSION_KEY_CLEANUP', True):
            session.pop('node_tokens', None)
        return None

    @app.after_request
    def _guard_session_cookie_size(response):  # noqa: ANN001
        # CR-05 需求⑤兜底：浏览器对单条 Cookie 的限制约 4093B，超限会被静默
        # 丢弃且不报错。超过 3500B 记 warning，便于再出现问题时快速定位。
        try:
            serializer = app.session_interface.get_signing_serializer(app)
            if serializer is None:
                return response
            payload = serializer.dumps(dict(session))
            if len(payload) > 3500:
                app.logger.warning(
                    '会话 Cookie 过大（%d 字节，浏览器上限约 4093）：%s %s',
                    len(payload), request.method, request.path)
        except Exception:  # 兜底：记录体积不得影响正常响应
            app.logger.debug('会话 Cookie 体积检查失败', exc_info=True)
        return response

    @app.before_request
    def _protect_csrf():  # noqa: ANN001
        if not app.config.get('CSRF_ENABLED', True):
            return None
        if request.method in _SAFE_METHODS:
            return None
        expected = session.get(CSRF_SESSION_KEY)
        sent = _submitted_token()
        if not expected or not sent or not hmac.compare_digest(str(expected), str(sent)):
            _csrf_failed()
        return None
