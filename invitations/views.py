import json

from django.views.generic import FormView, View
from django.views.generic.detail import SingleObjectMixin
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from braces.views import LoginRequiredMixin

from .forms import InviteForm, CleanEmailMixin
from .models import Invitation
from . import signals
from .exceptions import AlreadyInvited, AlreadyAccepted, UserRegisteredEmail
from .app_settings import app_settings
from .adapters import get_invitations_adapter


def handle_invitation_acceptance(invitation, view_class, request):
    """ trigger the signal and send out notiff message

        (to be called after the action following invitation link is completed 
         and after the invitation is marked as accepted)

        view_class, request: information for signal, to be gotten from the current view
    """
    signals.invite_accepted.send(
        sender=view_class, request=request, email=invitation.email
    )

    get_invitations_adapter().add_message(
        request,
        messages.SUCCESS,
        'invitations/messages/invite_accepted.txt',
        {'email': invitation.email}
    )


class SendInvite(LoginRequiredMixin, FormView):
    template_name = 'invitations/forms/_invite.html'
    form_class = InviteForm

    def form_valid(self, form):
        email = form.cleaned_data["email"]

        try:
            invite = form.save(email)
            invite.inviter = self.request.user
            invite.save()
            invite.send_invitation(self.request)
        except Exception:
            return self.form_invalid(form)

        return self.render_to_response(
            self.get_context_data(
                success_message='%s has been invited' % email))

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))


class SendJSONInvite(LoginRequiredMixin, View):
    http_method_names = [u'post']

    def dispatch(self, request, *args, **kwargs):
        if app_settings.ALLOW_JSON_INVITES:
            return super(SendJSONInvite, self).dispatch(
                request, *args, **kwargs)
        else:
            raise Http404

    def post(self, request, *args, **kwargs):
        status_code = 400
        invitees = json.loads(request.body.decode())
        response = {'valid': [], 'invalid': []}
        if isinstance(invitees, list):
            for invitee in invitees:
                try:
                    validate_email(invitee)
                    CleanEmailMixin().validate_invitation(invitee)
                    invite = Invitation.create(invitee, inviter=request.user)
                except(ValueError, KeyError):
                    pass
                except(ValidationError):
                    response['invalid'].append({
                        invitee: 'invalid email'})
                except(AlreadyAccepted):
                    response['invalid'].append({
                        invitee: 'already accepted'})
                except(AlreadyInvited):
                    response['invalid'].append(
                        {invitee: 'pending invite'})
                except(UserRegisteredEmail):
                    response['invalid'].append(
                        {invitee: 'user registered email'})
                else:
                    invite.send_invitation(request)
                    response['valid'].append({invitee: 'invited'})

        if response['valid']:
            status_code = 201

        return HttpResponse(
            json.dumps(response),
            status=status_code, content_type='application/json')


class AcceptInvite(SingleObjectMixin, View):
    form_class = InviteForm

    def get(self, *args, **kwargs):
        if app_settings.CONFIRM_INVITE_ON_GET:
            return self.post(*args, **kwargs)
        else:
            raise Http404()

    def post(self, *args, **kwargs):
        self.object = invitation = self.get_object()

        # Compatibility with older versions: return an HTTP 410 GONE if there
        # is an error. # Error conditions are: no key, expired key or
        # previously accepted key.
        if app_settings.GONE_ON_ACCEPT_ERROR and \
                (not invitation or
                 (invitation and (invitation.accepted or
                                  invitation.key_expired()))):
            return HttpResponse(status=410)

        # No invitation was found.
        if not invitation:
            # Newer behavior: show an error message and redirect.
            get_invitations_adapter().add_message(
                self.request,
                messages.ERROR,
                'invitations/messages/invite_invalid.txt')
            return redirect(app_settings.EXPIRED_REDIRECT)

        # The invitation was previously accepted, redirect to the login
        # view.
        if invitation.accepted:
            get_invitations_adapter().add_message(
                self.request,
                messages.ERROR,
                'invitations/messages/invite_already_accepted.txt',
                {'email': invitation.email})
            # Redirect to login since there's an account already.
            return redirect(app_settings.LOGIN_REDIRECT)

        # The key was expired.
        if invitation.key_expired():
            get_invitations_adapter().add_message(
                self.request,
                messages.ERROR,
                'invitations/messages/invite_expired.txt',
                {'email': invitation.email})
            return redirect(app_settings.EXPIRED_REDIRECT)

        # The invitation is valid! accept the email, let him finish the sign-up.
        get_invitations_adapter().stash_verified_email(self.request, invitation.email)

        return redirect(app_settings.SIGNUP_REDIRECT)

    def get_object(self, queryset=None):
        if queryset is None:
            queryset = self.get_queryset()
        try:
            return queryset.get(key=self.kwargs["key"].lower())
        except Invitation.DoesNotExist:
            return None

    def get_queryset(self):
        return Invitation.objects.all()
