# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import cast

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Field, Fieldset, Layout, Submit
from django import forms
from django.conf import settings
from django.contrib.auth import authenticate, password_validation
from django.contrib.auth.forms import SetPasswordForm as DjangoSetPasswordForm
from django.db import transaction
from django.middleware.csrf import rotate_token
from django.utils.functional import cached_property
from django.utils.html import escape
from django.utils.translation import activate, gettext, gettext_lazy, ngettext, pgettext

from weblate.accounts.auth import try_get_user
from weblate.accounts.captcha import MathCaptcha
from weblate.accounts.models import AuditLog, Profile
from weblate.accounts.notifications import (
    NOTIFICATIONS,
    SCOPE_ADMIN,
    SCOPE_ALL,
    SCOPE_CHOICES,
    SCOPE_PROJECT,
    SCOPE_WATCHED,
)
from weblate.accounts.utils import (
    adjust_session_expiry,
    cycle_session_keys,
    get_all_user_mails,
    invalidate_reset_codes,
)
from weblate.auth.models import AuthenticatedHttpRequest, Group, User
from weblate.lang.models import Language
from weblate.logger import LOGGER
from weblate.trans.defines import FULLNAME_LENGTH
from weblate.trans.models import Component, Project
from weblate.utils import messages
from weblate.utils.forms import (
    EmailField,
    QueryField,
    SortedSelect,
    SortedSelectMultiple,
    UsernameField,
)
from weblate.utils.ratelimit import check_rate_limit, get_rate_setting, reset_rate_limit
from weblate.utils.validators import validate_fullname


class UniqueEmailMixin(forms.Form):
    validate_unique_mail = False

    def clean_email(self):
        """Validate whether email address is not already in use."""
        self.cleaned_data["email_user"] = None
        mail = self.cleaned_data["email"]
        users = User.objects.filter(
            email=mail,
            is_active=True,
            is_bot=False,
        )
        if not users:
            users = User.objects.filter(
                social_auth__verifiedemail__email__iexact=mail,
                is_active=True,
                is_bot=False,
            )
        if users:
            self.cleaned_data["email_user"] = users[0]
            if self.validate_unique_mail:
                raise forms.ValidationError(
                    gettext(
                        "This e-mail address is already in use. "
                        "Please supply a different e-mail address."
                    )
                )
        return self.cleaned_data["email"]


class PasswordField(forms.CharField):
    """Password field."""

    def __init__(self, new_password: bool = False, **kwargs) -> None:
        kwargs["widget"] = forms.PasswordInput(
            attrs={
                "autocomplete": "new-password" if new_password else "current-password"
            },
            render_value=False,
        )
        kwargs["max_length"] = 256
        kwargs["strip"] = False
        super().__init__(**kwargs)


class UniqueUsernameField(UsernameField):
    def clean(self, value):
        """Username validation, requires a unique name."""
        if value is None:
            return None
        if value is not None:
            existing = User.objects.filter(username=value)
            if existing.exists() and value != self.valid:
                raise forms.ValidationError(
                    gettext("This username is already taken. Please choose another.")
                )

        return super().clean(value)


class FullNameField(forms.CharField):
    default_validators = [validate_fullname]

    def __init__(self, *args, **kwargs) -> None:
        kwargs["max_length"] = FULLNAME_LENGTH
        kwargs["label"] = gettext_lazy("Full name")
        kwargs["help_text"] = gettext_lazy(
            "Name is also used in version control commits."
        )
        kwargs["required"] = True
        super().__init__(*args, **kwargs)


class ProfileBaseForm(forms.ModelForm):
    @classmethod
    def from_request(cls, request: AuthenticatedHttpRequest):
        if request.method == "POST":
            return cls(request.POST, instance=request.user.profile)
        return cls(instance=request.user.profile)

    def add_error(self, field, error) -> None:
        if field is None and hasattr(error, "error_dict"):
            # Skip errors from model clean method on unknown fields as
            # this is partial form. This is really bound to how Profile.clean
            # behaves.
            ignored_fields = ("dashboard_component_list", "dashboard_view")
            for field_name in error.error_dict:
                if field_name in ignored_fields and not hasattr(self, field_name):
                    return
        super().add_error(field, error)


class LanguagesForm(ProfileBaseForm):
    """User profile editing."""

    class Meta:
        model = Profile
        fields = ("language", "languages", "secondary_languages")
        widgets = {
            "language": SortedSelect,
            "languages": SortedSelectMultiple,
            "secondary_languages": SortedSelectMultiple,
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Remove empty choice from the form. We need it at the database level
        # to initialize user profile, but it is filled in later based on
        # languages configured in the browser.
        self.fields["language"].choices = [
            choice for choice in self.fields["language"].choices if choice[0]
        ]
        # Limit languages to ones which have translation, do this by generating choices
        # instead of queryset as the queryset would be evaluated twice as
        # ModelChoiceField copies the queryset
        languages = Language.objects.have_translation()
        choices = list(languages.as_choices(use_code=False))
        self.fields["languages"].choices = choices
        self.fields["secondary_languages"].choices = choices
        self.helper = FormHelper(self)
        self.helper.disable_csrf = True
        self.helper.form_tag = False

    def save(self, commit=True) -> None:
        super().save(commit=commit)
        # Activate selected language
        activate(self.cleaned_data["language"])


class CommitForm(ProfileBaseForm):
    commit_email = forms.ChoiceField(
        label=gettext_lazy("Commit e-mail"),
        choices=[("", gettext_lazy("Use account e-mail address"))],
        help_text=gettext_lazy(
            "Used in version control commits. The address will stay in the repository forever once changes are committed by Weblate."
        ),
        required=False,
        widget=forms.RadioSelect,
    )

    class Meta:
        model = Profile
        fields = ("commit_email",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        commit_emails = get_all_user_mails(self.instance.user, filter_deliverable=False)
        site_commit_email = self.instance.get_site_commit_email()
        if site_commit_email:
            if not settings.PRIVATE_COMMIT_EMAIL_OPT_IN:
                self.fields["commit_email"].choices = [("", site_commit_email)]
            else:
                commit_emails.add(site_commit_email)

        self.fields["commit_email"].choices += [(x, x) for x in sorted(commit_emails)]

        self.helper = FormHelper(self)
        self.helper.disable_csrf = True
        self.helper.form_tag = False


class ProfileForm(ProfileBaseForm):
    """User profile editing."""

    public_email = forms.ChoiceField(
        label=gettext_lazy("Public e-mail"),
        choices=[("", gettext_lazy("Do not publicly display e-mail address"))],
        required=False,
    )

    class Meta:
        model = Profile
        fields = (
            "website",
            "public_email",
            "liberapay",
            "codesite",
            "github",
            "fediverse",
            "twitter",
            "linkedin",
            "location",
            "company",
        )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        emails = get_all_user_mails(self.instance.user)

        self.fields["public_email"].choices += [(x, x) for x in sorted(emails)]

        self.helper = FormHelper(self)
        self.helper.disable_csrf = True
        self.helper.form_tag = False


class SubscriptionForm(ProfileBaseForm):
    """User watched projects management."""

    class Meta:
        model = Profile
        fields = (
            "auto_watch",
            "watched",
        )
        widgets = {"watched": forms.SelectMultiple}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        user = kwargs["instance"].user
        self.fields["watched"].required = False
        self.fields["watched"].queryset = user.allowed_projects
        self.helper = FormHelper(self)
        self.helper.disable_csrf = True
        self.helper.form_tag = False


class UserSettingsForm(ProfileBaseForm):
    """User settings form."""

    class Meta:
        model = Profile
        fields = (
            "theme",
            "hide_completed",
            "translate_mode",
            "zen_mode",
            "nearby_strings",
            "secondary_in_zen",
            "hide_source_secondary",
            "editor_link",
            "special_chars",
        )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["special_chars"].strip = False
        self.helper = FormHelper(self)
        self.helper.disable_csrf = True
        self.helper.form_tag = False


class DashboardSettingsForm(ProfileBaseForm):
    """Dashboard settings form."""

    class Meta:
        model = Profile
        fields = ("dashboard_view", "dashboard_component_list")
        widgets = {
            "dashboard_view": forms.RadioSelect,
            "dashboard_component_list": forms.HiddenInput,
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.disable_csrf = True
        self.helper.form_tag = False
        component_lists = self.instance.allowed_dashboard_component_lists
        self.fields["dashboard_component_list"].queryset = component_lists
        choices = [
            choice
            for choice in self.fields["dashboard_view"].choices
            if choice[0] != Profile.DASHBOARD_COMPONENT_LIST
        ]
        if not component_lists:
            choices = [
                choice
                for choice in choices
                if choice[0] != Profile.DASHBOARD_COMPONENT_LISTS
            ]
        choices.extend(
            (100 + clist.id, gettext("Component list: %s") % clist.name)
            for clist in component_lists
        )
        self.fields["dashboard_view"].choices = choices
        if (
            self.instance.dashboard_view == Profile.DASHBOARD_COMPONENT_LIST
            and self.instance.dashboard_component_list
        ):
            self.initial["dashboard_view"] = (
                100 + self.instance.dashboard_component_list_id
            )

    def clean(self) -> None:
        view = self.cleaned_data.get("dashboard_view")
        if view and view >= 100:
            self.cleaned_data["dashboard_view"] = Profile.DASHBOARD_COMPONENT_LIST
            view -= 100
            for clist in self.instance.allowed_dashboard_component_lists:
                if clist.id == view:
                    self.cleaned_data["dashboard_component_list"] = clist
                    break


class UserForm(forms.ModelForm):
    """User information form."""

    username = UniqueUsernameField()
    email = forms.ChoiceField(
        label=gettext_lazy("Account e-mail"),
        help_text=gettext_lazy(
            "Used for e-mail notifications and as a commit e-mail if it is not configured below."
        ),
        choices=(("", ""),),
        required=True,
        widget=forms.RadioSelect,
    )
    full_name = FullNameField()

    class Meta:
        model = User
        fields = ("username", "full_name", "email")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        emails = get_all_user_mails(self.instance)

        self.fields["email"].choices = [(x, x) for x in sorted(emails)]
        self.fields["username"].valid = self.instance.username

        self.helper = FormHelper(self)
        self.helper.disable_csrf = True
        self.helper.form_tag = False

    @classmethod
    def from_request(cls, request: AuthenticatedHttpRequest):
        if request.method == "POST":
            return cls(request.POST, instance=request.user)
        return cls(instance=request.user)

    def audit(self, request: AuthenticatedHttpRequest) -> None:
        orig = User.objects.get(pk=self.instance.pk)
        for attr in ("username", "full_name", "email"):
            orig_attr = getattr(orig, attr)
            new_attr = getattr(self.instance, attr)
            if orig_attr != new_attr:
                AuditLog.objects.create(
                    orig, request, attr, old=orig_attr, new=new_attr
                )


class ContactForm(forms.Form):
    """Form for contacting site owners."""

    subject = forms.CharField(
        label=gettext_lazy("Subject"), required=True, max_length=100
    )
    name = forms.CharField(
        label=gettext_lazy("Your name"), required=True, max_length=FULLNAME_LENGTH
    )
    email = EmailField(label=gettext_lazy("Your e-mail"), required=True)
    message = forms.CharField(
        label=gettext_lazy("Message"),
        required=True,
        help_text=gettext_lazy(
            "Please contact us in English, otherwise we might "
            "be unable to process your request."
        ),
        max_length=2000,
        widget=forms.Textarea,
    )


class EmailForm(UniqueEmailMixin):
    """Email change form."""

    required_css_class = "required"
    error_css_class = "error"

    email = EmailField(
        label=gettext_lazy("E-mail"),
        help_text=gettext_lazy("E-mail with a confirmation link will be sent here."),
    )


class RegistrationForm(EmailForm):
    """Registration form."""

    required_css_class = "required"
    error_css_class = "error"

    username = UniqueUsernameField()
    # This has to be without underscore for social-auth
    fullname = FullNameField()

    def __init__(self, request=None, *args, **kwargs) -> None:
        # The 'request' parameter is set for custom auth use by subclasses.
        # The form data comes in via the standard 'data' kwarg.
        self.request = request
        super().__init__(*args, **kwargs)

    def clean(self):
        if not check_rate_limit("registration", self.request):
            lockout_period = get_rate_setting("registration", "LOCKOUT") // 60
            raise forms.ValidationError(
                ngettext(
                    (
                        "Too many failed registration attempts from this location. "
                        "Please try again in %d minute."
                    ),
                    (
                        "Too many failed registration attempts from this location. "
                        "Please try again in %d minutes."
                    ),
                    lockout_period,
                )
                % lockout_period
            )
        return self.cleaned_data


class SetPasswordForm(DjangoSetPasswordForm):
    new_password1 = PasswordField(
        label=gettext_lazy("New password"),
        help_text=password_validation.password_validators_help_text_html(),
        new_password=True,
    )
    new_password2 = PasswordField(
        label=gettext_lazy("New password confirmation"),
        new_password=True,
    )

    @transaction.atomic
    def save(self, request: AuthenticatedHttpRequest, delete_session=False) -> None:
        AuditLog.objects.create(
            self.user,
            request,
            "password",
            password=self.user.password,
            method="changed" if self.user.has_usable_password() else "configured",
        )
        # Change the password
        password = self.cleaned_data["new_password1"]
        self.user.set_password(password)
        self.user.save(update_fields=["password"])

        # Updating the password logs out all other sessions for the user
        # except the current one and change key for current session
        cycle_session_keys(request, self.user)

        # Invalidate password reset codes
        invalidate_reset_codes(self.user)

        if delete_session:
            request.session.flush()

        messages.success(request, gettext("Your password has been changed."))


class CaptchaForm(forms.Form):
    captcha = forms.IntegerField(required=True)

    def __init__(
        self, request: AuthenticatedHttpRequest, form=None, data=None, *args, **kwargs
    ) -> None:
        super().__init__(data, *args, **kwargs)
        self.fresh = False
        self.request = request
        self.form = form

        if data is None or "captcha" not in request.session:
            self.generate_captcha()
            self.fresh = True
        else:
            self.mathcaptcha = MathCaptcha.unserialize(request.session["captcha"])
            self.set_label()

    def set_label(self) -> None:
        # Set correct label
        self.fields["captcha"].label = (
            pgettext(
                "Question for a mathematics-based CAPTCHA, "
                "the %s is an arithmetic problem",
                "What is %s?",
            )
            % self.mathcaptcha.display
        )
        if self.is_bound:
            self["captcha"].label = cast(str, self.fields["captcha"].label)

    def generate_captcha(self) -> None:
        self.mathcaptcha = MathCaptcha()
        self.request.session["captcha"] = self.mathcaptcha.serialize()
        self.set_label()

    def clean_captcha(self) -> None:
        """Validate CAPTCHA."""
        if self.fresh or not self.mathcaptcha.validate(self.cleaned_data["captcha"]):
            self.generate_captcha()
            rotate_token(self.request)
            raise forms.ValidationError(
                # Translators: Shown on wrong answer to the mathematics-based CAPTCHA
                gettext("That was not correct, please try again.")
            )

        mail = self.form.cleaned_data["email"] if self.form.is_valid() else "NONE"

        LOGGER.info(
            "Correct CAPTCHA for %s (%s = %s)",
            mail,
            self.mathcaptcha.question,
            self.cleaned_data["captcha"],
        )

    def cleanup_session(self, request: AuthenticatedHttpRequest) -> None:
        del request.session["captcha"]


class EmptyConfirmForm(forms.Form):
    def __init__(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> None:
        self.request = request
        self.user = request.user
        if "user" in kwargs:
            self.user = kwargs.pop("user")
        super().__init__(*args, **kwargs)


class PasswordConfirmForm(EmptyConfirmForm):
    password = PasswordField(
        label=gettext_lazy("Current password"),
        help_text=gettext_lazy("Leave empty if you have not yet set a password."),
        required=False,
    )

    def clean_password(self) -> None:
        cur_password = self.cleaned_data["password"]
        valid = False
        if self.user.has_usable_password():
            valid = self.user.check_password(cur_password)
        elif not cur_password:
            valid = True
        if not valid:
            rotate_token(self.request)
            raise forms.ValidationError(
                gettext("You have entered an invalid password.")
            )


class ResetForm(EmailForm):
    def clean_email(self):
        if self.cleaned_data["email"] == "noreply@weblate.org":
            raise forms.ValidationError(
                "No password reset for deleted or anonymous user."
            )
        return super().clean_email()


class LoginForm(forms.Form):
    username = forms.CharField(max_length=254, label=gettext_lazy("Username or e-mail"))
    password = PasswordField(label=gettext_lazy("Password"))

    error_messages = {
        "invalid_login": gettext_lazy(
            "Please enter the correct username and password."
        ),
        "inactive": gettext_lazy("This account is inactive."),
    }

    def __init__(self, request=None, *args, **kwargs) -> None:
        # The 'request' parameter is set for custom auth use by subclasses.
        # The form data comes in via the standard 'data' kwarg.
        self.request = request
        self.user_cache: User | None = None
        super().__init__(*args, **kwargs)

    def clean(self):
        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")

        if username and password:
            if not check_rate_limit("login", self.request):
                lockout_period = get_rate_setting("login", "LOCKOUT") // 60
                raise forms.ValidationError(
                    ngettext(
                        (
                            "Too many authentication attempts from this location. "
                            "Please try again in %d minute."
                        ),
                        (
                            "Too many authentication attempts from this location. "
                            "Please try again in %d minutes."
                        ),
                        lockout_period,
                    )
                    % lockout_period
                )
            self.user_cache = cast(
                User | None,
                authenticate(self.request, username=username, password=password),
            )
            if self.user_cache is None:
                for user in try_get_user(username, True):
                    audit = AuditLog.objects.create(
                        user,
                        self.request,
                        "failed-auth",
                        method="password",
                        name=username,
                    )
                    audit.check_rate_limit(self.request)
                rotate_token(self.request)
                raise forms.ValidationError(
                    self.error_messages["invalid_login"], code="invalid_login"
                )
            if not self.user_cache.is_active or self.user_cache.is_bot:
                raise forms.ValidationError(
                    self.error_messages["inactive"], code="inactive"
                )
            AuditLog.objects.create(
                self.user_cache, self.request, "login", method="password", name=username
            )
            adjust_session_expiry(self.request)
            reset_rate_limit("login", self.request)
        return self.cleaned_data

    def get_user(self):
        return self.user_cache


class AdminLoginForm(LoginForm):
    def clean(self):
        data = super().clean()
        if self.user_cache and not self.user_cache.is_superuser:
            raise forms.ValidationError(
                self.error_messages["inactive"], code="inactive"
            )
        return data


class NotificationForm(forms.Form):
    scope = forms.ChoiceField(
        choices=SCOPE_CHOICES, widget=forms.HiddenInput, required=True
    )
    project = forms.ModelChoiceField(
        widget=forms.HiddenInput, queryset=Project.objects.none(), required=False
    )
    component = forms.ModelChoiceField(
        widget=forms.HiddenInput, queryset=Component.objects.none(), required=False
    )

    def __init__(
        self, *, user, show_default, removable, subscriptions, is_active, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.user = user
        self.is_active = is_active
        self.removable = removable
        self.show_default = show_default
        self.fields["project"].queryset = user.allowed_projects
        self.fields["component"].queryset = Component.objects.filter_access(user)
        language_fields = []
        component_fields = []
        for field, notification_cls in self.notification_fields():
            self.fields[field] = forms.ChoiceField(
                label=notification_cls.verbose,
                choices=self.get_choices(notification_cls, show_default),
                required=False,
                initial=self.get_initial(notification_cls, subscriptions, show_default),
            )
            if notification_cls.filter_languages:
                language_fields.append(field)
            else:
                component_fields.append(field)
        self.helper = FormHelper(self)
        self.helper.disable_csrf = True
        self.helper.form_tag = False
        self.helper.label_class = "col-md-3"
        self.helper.field_class = "col-md-9"
        self.helper.layout = Layout(
            "scope",
            "project",
            "component",
            Fieldset(
                gettext("Component wide notifications"),
                HTML(escape(self.get_help_component())),
                *component_fields,
            ),
            Fieldset(
                gettext("Translation notifications"),
                HTML(escape(self.get_help_translation())),
                *language_fields,
            ),
        )

    @staticmethod
    def notification_fields():
        for notification_cls in NOTIFICATIONS:
            yield (f"notify-{notification_cls.get_name()}", notification_cls)

    @staticmethod
    def get_initial(notification_cls, subscriptions, show_default):
        return subscriptions.get(notification_cls.get_name(), -1 if show_default else 0)

    @staticmethod
    def get_choices(notification_cls, show_default):
        result = []
        if show_default:
            result.append((-1, gettext("Use default setting")))
        result.extend(notification_cls.get_freq_choices())
        return result

    @cached_property
    def form_params(self):
        if self.is_bound:
            self.is_valid()
            return self.cleaned_data
        return self.initial

    def get_form_param(self, name: str, default):
        result = self.form_params.get(name)
        if result is not None:
            return result
        return self.initial.get(name, default)

    @cached_property
    def form_scope(self):
        return int(self.get_form_param("scope", SCOPE_WATCHED))

    @cached_property
    def form_project(self):
        return self.get_form_param("project", None)

    @cached_property
    def form_component(self):
        return self.get_form_param("component", None)

    def get_name(self):
        scope = self.form_scope
        if scope == SCOPE_ALL:
            return gettext("Other projects")
        if scope == SCOPE_WATCHED:
            return gettext("Watched projects")
        if scope == SCOPE_ADMIN:
            return gettext("Managed projects")
        if scope == SCOPE_PROJECT:
            return gettext("Project: {}").format(self.form_project)
        return gettext("Component: {}").format(self.form_component)

    def get_help_component(self):
        scope = self.form_scope
        if scope == SCOPE_ALL:
            return gettext(
                "You will receive a notification for every such event"
                " in non-watched projects."
            )
        if scope == SCOPE_WATCHED:
            return gettext(
                "You will receive a notification for every such event"
                " in your watched projects."
            )
        if scope == SCOPE_ADMIN:
            return gettext(
                "You will receive a notification for every such event"
                " in projects where you have admin permissions."
            )
        if scope == SCOPE_PROJECT:
            return gettext(
                "You will receive a notification for every such event in %(project)s."
            ) % {"project": self.form_project}
        return gettext(
            "You will receive a notification for every such event in %(component)s."
        ) % {"component": self.form_component}

    def get_help_translation(self):
        scope = self.form_scope
        if scope == SCOPE_ALL:
            return gettext(
                "You will only receive these notifications for your translated "
                "languages in non-watched projects."
            )
        if scope == SCOPE_WATCHED:
            return gettext(
                "You will only receive these notifications for your translated "
                "languages in your watched projects."
            )
        if scope == SCOPE_ADMIN:
            return gettext(
                "You will only receive these notifications for your translated "
                "languages in projects where you have admin permissions."
            )
        if scope == SCOPE_PROJECT:
            return gettext(
                "You will only receive these notifications for your"
                " translated languages in %(project)s."
            ) % {"project": self.form_project}
        return gettext(
            "You will only receive these notifications for your"
            " translated languages in %(component)s."
        ) % {"component": self.form_component}

    def save(self) -> None:
        # Lookup for this form
        lookup = {
            "scope": self.cleaned_data["scope"],
            "project": self.cleaned_data["project"],
            "component": self.cleaned_data["component"],
        }
        handled = set()
        for field, notification_cls in self.notification_fields():
            frequency = self.cleaned_data[field]
            # We do not store removed field, defaults or disabled default subscriptions
            if frequency in {"", "-1"} or (frequency == "0" and not self.show_default):
                continue
            # Create/Get from database
            subscription, _created = self.user.subscription_set.update_or_create(
                notification=notification_cls.get_name(),
                defaults={"frequency": frequency},
                **lookup,
            )
            handled.add(subscription.pk)
        # Delete stale subscriptions
        self.user.subscription_set.filter(**lookup).exclude(pk__in=handled).delete()


class UserSearchForm(forms.Form):
    """User searching form."""

    q = QueryField(parser="user")
    sort_by = forms.CharField(required=False, widget=forms.HiddenInput)

    sort_choices = {
        "username": gettext_lazy("Username"),
        "full_name": gettext_lazy("Full name"),
        "date_joined": gettext_lazy("Date joined"),
        "profile__translated": gettext_lazy("Translations made"),
        "profile__suggested": gettext_lazy("Suggestions made"),
        "profile__commented": gettext_lazy("Comments made"),
        "profile__uploaded": gettext_lazy("Screenshots uploaded"),
    }
    sort_values = set(sort_choices) | {f"-{val}" for val in sort_choices}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.helper = FormHelper(self)
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.layout = Layout(
            Div(
                Field("q", template="snippets/user-query-field.html"),
                Field("sort_by", template="snippets/user-sort-field.html"),
                css_class="btn-toolbar",
                role="toolbar",
            ),
        )

    def clean_sort_by(self):
        sort_by = self.cleaned_data.get("sort_by")
        if sort_by:
            if sort_by not in self.sort_values:
                raise forms.ValidationError(gettext("Chosen sorting is not supported."))
            return sort_by
        return None


class AdminUserSearchForm(UserSearchForm):
    q = QueryField(parser="superuser")


class GroupChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.long_name()


class GroupAddForm(forms.Form):
    add_group = GroupChoiceField(
        label=gettext_lazy("Add user to a team"),
        queryset=Group.objects.prefetch_related("defining_project").order(),
        required=True,
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_class = "form-inline"
        self.helper.field_template = "bootstrap3/layout/inline_field.html"
        self.helper.layout = Layout(
            "add_group",
            Submit("add_group_button", gettext("Add team")),
        )


class GroupRemoveForm(forms.Form):
    remove_group = forms.ModelChoiceField(queryset=Group.objects.all(), required=True)
