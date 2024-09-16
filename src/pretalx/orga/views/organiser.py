import logging

from allauth.socialaccount.models import SocialApp
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, TemplateView
from django_context_decorator import context

from pretalx.common.exceptions import SendMailException
from pretalx.common.text.phrases import phrases
from pretalx.common.views import CreateOrUpdateView
from pretalx.common.views import is_form_bound
from pretalx.common.views.mixins import ActionConfirmMixin, PermissionRequired
from pretalx.event.forms import OrganiserForm, TeamForm, TeamInviteForm
from pretalx.event.models.organiser import (
    Organiser,
    Team,
    TeamInvite,
    check_access_permissions,
)
from pretalx.orga.forms.sso_client_form import SSOClientForm

logger = logging.getLogger(__name__)


class TeamMixin:
    def _get_team(self):
        if "pk" in getattr(self, "kwargs", {}):
            return get_object_or_404(
                self.request.organiser.teams.all(), pk=self.kwargs["pk"]
            )

    def get_object(self, queryset=None):
        return self._get_team()

    @context
    @cached_property
    def team(self):
        return self._get_team()

    @cached_property
    def object(self):
        return self.get_object()


class TeamDetail(PermissionRequired, TeamMixin, CreateOrUpdateView):
    permission_required = "orga.change_teams"
    template_name = "orga/settings/team_detail.html"
    form_class = TeamForm
    model = Team

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organiser"] = self.request.organiser
        return kwargs

    def get_object(self, queryset=None):
        if "pk" not in self.kwargs:
            return None
        return super().get_object(queryset=queryset)

    def get_permission_object(self):
        if "pk" not in self.kwargs:
            return self.request.organiser
        return self.get_object()

    @context
    @cached_property
    def invite_form(self):
        is_bound = (
            self.request.method == "POST" and self.request.POST.get("form") == "invite"
        )
        return TeamInviteForm(self.request.POST if is_bound else None, prefix="invite")

    @context
    @cached_property
    def members(self):
        if not self.team or not self.team.pk:
            return []
        return self.team.members.all().order_by("name")

    def post(self, *args, **kwargs):
        if self.invite_form.is_bound:
            if self.invite_form.is_valid():
                invites = self.invite_form.save(team=self.team)
                if len(invites) == 1:
                    messages.success(self.request, _("The invitation has been sent."))
                else:
                    messages.success(self.request, _("The invitations have been sent."))
            else:
                for error in self.invite_form.errors.values():
                    messages.error(self.request, "\n".join(error))
            return redirect(self.request.path)
        return super().post(*args, **kwargs)

    def form_valid(self, form):
        created = not bool(form.instance.pk)
        warnings = []
        try:
            with transaction.atomic():
                form.save()
                if not created:
                    warnings = check_access_permissions(self.request.organiser)
        except Exception as e:
            # We can't save because we would break the organiser's permissions,
            # e.g. leave an event or the entire organiser orphaned.
            messages.error(self.request, str(e))
            return self.get(self.request, *self.args, **self.kwargs)
        if warnings:
            for warning in warnings:
                messages.warning(self.request, warning)
        if created:
            messages.success(self.request, _("The team has been created."))
        elif form.has_changed():
            messages.success(self.request, _("The settings have been saved."))
        return super().form_valid(form)

    def get_success_url(self):
        if "pk" not in self.kwargs:
            return self.request.organiser.orga_urls.base
        return self.request.GET.get("next", self.request.path)


class TeamDelete(PermissionRequired, TeamMixin, ActionConfirmMixin, DetailView):
    permission_required = "orga.change_teams"

    def get_permission_object(self):
        return self._get_team()

    def get_object(self, queryset=None):
        team = super().get_object()
        if "user_pk" in self.kwargs:
            return team.members.filter(pk=self.kwargs.get("user_pk")).first()
        return team

    def action_object_name(self):
        if "user_pk" in self.kwargs:
            return _("Team member") + f": {self.get_object().name}"
        return _("Team") + f": {self.get_object().name}"

    @property
    def action_back_url(self):
        return self._get_team().orga_urls.base

    @context
    @cached_property
    def member(self):
        member = self.get_object()
        return member if member != self.team else None

    def post(self, request, *args, **kwargs):
        warnings = []
        try:
            with transaction.atomic():
                if "user_pk" in self.kwargs:
                    self.team.members.remove(self.get_object())
                    warnings = check_access_permissions(self.request.organiser)
                    messages.success(
                        request, _("The member was removed from the team.")
                    )
                else:
                    self.get_object().delete()
                    warnings = check_access_permissions(self.request.organiser)
                    messages.success(request, _("The team was removed."))
        except Exception as e:
            messages.error(request, str(e))
            return self.get(request, *args, **kwargs)
        if warnings:
            for warning in warnings:
                messages.warning(request, warning)
        return redirect(self.request.organiser.orga_urls.base)


class InviteMixin:
    def get_permission_object(self):
        return self.request.organiser

    @context
    @cached_property
    def team(self):
        return get_object_or_404(
            self.request.organiser.teams.all(), pk=self.object.team.pk
        )

    @cached_property
    def object(self):
        return get_object_or_404(
            self.team.invites.all(),
            pk=self.kwargs["pk"],
        )


class TeamUninvite(InviteMixin, PermissionRequired, ActionConfirmMixin, DetailView):
    model = TeamInvite
    permission_required = "orga.change_teams"
    action_title = _("Retract invitation")
    action_text = _("Are you sure you want to retract the invitation to this user?")

    def action_object_name(self):
        return self.get_object().email

    @property
    def action_back_url(self):
        return self.get_object().team.orga_urls.base

    def post(self, request, *args, **kwargs):
        self.get_object().delete()
        messages.success(request, _("The team invitation was retracted."))
        return redirect(self.request.organiser.orga_urls.base)


class TeamResend(InviteMixin, PermissionRequired, ActionConfirmMixin, DetailView):
    model = TeamInvite
    permission_required = "orga.change_teams"
    action_title = _("Resend invitation")
    action_text = _("Are you sure you want to resend the invitation to this user?")
    action_confirm_color = "success"
    action_confirm_icon = "envelope"
    action_confirm_label = phrases.base.send

    def action_object_name(self):
        return self.get_object().email

    @property
    def action_back_url(self):
        return self.get_object().team.orga_urls.base

    def post(self, request, *args, **kwargs):
        self.get_object().send()
        messages.success(request, _("The team invitation was sent again."))
        return redirect(self.request.organiser.orga_urls.base)


class TeamResetPassword(PermissionRequired, ActionConfirmMixin, TemplateView):
    model = Team
    permission_required = "orga.change_teams"
    action_confirm_icon = "key"
    action_confirm_label = phrases.base.password_reset_heading
    action_title = phrases.base.password_reset_heading
    action_text = _(
        "Do your really want to reset this user’s password? They won’t be able to log in until they set a new password."
    )

    def action_object_name(self):
        return f"{self.user.get_display_name()} ({self.user.email})"

    @property
    def action_back_url(self):
        return self.team.orga_urls.base

    def get_permission_object(self):
        return self.request.organiser

    @context
    @cached_property
    def team(self):
        return get_object_or_404(
            self.request.organiser.teams.all(), pk=self.kwargs["pk"]
        )

    @context
    @cached_property
    def user(self):
        return get_object_or_404(self.team.members.all(), pk=self.kwargs["user_pk"])

    def post(self, request, *args, **kwargs):
        try:
            self.user.reset_password(event=None, user=self.request.user)
            messages.success(self.request, phrases.orga.password_reset_success)
        except SendMailException:  # pragma: no cover
            messages.error(self.request, phrases.orga.password_reset_fail)
        return redirect(self.request.organiser.orga_urls.base)


class OrganiserDetail(PermissionRequired, CreateOrUpdateView):
    template_name = "orga/organiser/detail.html"
    model = Organiser
    permission_required = "orga.change_organiser_settings"
    form_class = OrganiserForm

    @context
    @cached_property
    def teams(self):
        if not self.object:
            return []
        return self.request.organiser.teams.all().order_by("-all_events", "-id")

    def get_object(self, queryset=None):
        return getattr(self.request, "organiser", None)

    @cached_property
    def object(self):
        return self.get_object()

    def get_success_url(self):
        messages.success(self.request, _("Saved!"))
        return self.request.path

    @context
    @cached_property
    def sso_client_form(self):
        organiser = self.kwargs.get("organiser", None)
        if self.request.POST.get("form") == "remove_sso_client":
            bind = is_form_bound(self.request, "remove_sso_client")
        else:
            bind = is_form_bound(self.request, "sso_client")
        return SSOClientForm(
            provider_id=organiser,
            data=self.request.POST if bind else None,
        )

    def save_sso_client(self, request, *args, **kwargs):
        try:
            self.sso_client_form.save(organiser=self.kwargs.get("organiser", None))
        except Exception as e:
            logger.error(
                f"Error saving SSO client for organiser {self.kwargs.get('organiser', None)}: {e}"
            )
            messages.error(request, _("An error occurred: ") + str(e))
            return redirect(self.request.path)
        return redirect(self.get_success_url())

    def post(self, request, *args, **kwargs):
        try:
            if self.is_remove_sso_client_request(request):
                return self.handle_remove_sso_client(request)
            elif self.is_sso_client_request(request):
                return self.handle_sso_client(request, *args, **kwargs)
            else:
                self.set_object()
                return super().post(request, *args, **kwargs)
        except Exception as e:
            messages.error(request, _("An error occurred: ") + str(e))
            return redirect(self.request.path)

    def is_remove_sso_client_request(self, request):
        return (
            is_form_bound(self.request, "remove_sso_client")
            and request.POST.get("form") == "remove_sso_client"
        )

    def handle_remove_sso_client(self, request):
        provider_id = self.kwargs.get("organiser")
        try:
            social_app = SocialApp.objects.get(provider=provider_id)
            social_app.delete()
        except SocialApp.DoesNotExist:
            messages.error(request, _("The key does not exist."))
        return redirect(self.request.path)

    def is_sso_client_request(self, request):
        return (
            is_form_bound(self.request, "sso_client")
            and request.POST.get("form") == "sso_client"
            and self.sso_client_form.is_valid()
        )

    def handle_sso_client(self, request, *args, **kwargs):
        return self.save_sso_client(request, *args, **kwargs)


class OrganiserDelete(PermissionRequired, ActionConfirmMixin, DetailView):
    permission_required = "person.is_administrator"
    model = Organiser
    action_text = (
        _(
            "ALL related data for ALL events, such as proposals, and speaker profiles, and uploads, "
            "will also be deleted and cannot be restored."
        )
        + " "
        + phrases.base.delete_warning
    )

    def get_object(self, queryset=None):
        return getattr(self.request, "organiser", None)

    def action_object_name(self):
        return _("Organiser") + f": {self.get_object().name}"

    @property
    def action_back_url(self):
        return self.get_object().orga_urls.base

    def post(self, *args, **kwargs):
        organiser = self.get_object()
        organiser.shred(person=self.request.user)
        return HttpResponseRedirect(reverse("orga:event.list"))
