import json
import subprocess
from django.shortcuts import render
from rest_framework import viewsets
from datetime import datetime, timedelta
from rest_framework.response import Response
from rest_framework.decorators import action, api_view, permission_classes, authentication_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken, AccessToken
from rest_framework_simplejwt.authentication import JWTAuthentication

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.mail import send_mail, EmailMultiAlternatives
from healthconnect import settings
import git
import os
import logging
from threading import Thread

from .models import *
from rest_framework_simplejwt.views import TokenObtainPairView
from .serializers import *
from rest_framework import status

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from six import text_type
from django.contrib.sites.shortcuts import get_current_site  
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode  
# Create your views here.
class TokenGenerator(PasswordResetTokenGenerator):  
    def _make_hash_value(self, user, timestamp):  
        return (  
            text_type(user.pk) + text_type(timestamp)
        )  
generate_token = TokenGenerator()


class MyTokenObtainPairView(TokenObtainPairView):
    serializer_class = MyTokenObtainPairSerializer

@api_view(['GET', 'OPTIONS'])
def activate(request, uidb64, token):  
    User = get_user_model()
    try:  
        uid = force_str(urlsafe_base64_decode(uidb64))  
        user = User.objects.get(pk=uid)  
    except(TypeError, ValueError, OverflowError, User.DoesNotExist):  
        user = None
    if user is not None and generate_token.check_token(user, token):  
        user.is_active = True  
        user.save()  
        return Response({'success': 'Account activated successfully.'}, status=200)
    else:  
        return Response({'error': 'Invalid activation link.'}, status=400)


# Create your views here.
@api_view(['POST', 'OPTIONS'])
@authentication_classes([])
def register(request):
    """
    Register a new user.

    Args:
        request (Request): The request object containing user data.

    Returns:
        Response: A response object with success or error message.
    """
    if request.method == 'POST':
        user_data = request.data.copy()
        if 'first_name' not in user_data or 'last_name' not in user_data or 'email' not in user_data or 'password' not in user_data:
            return Response({'error': 'Missing required fields'}, status=400)
        
        user = User.objects.filter(email=user_data['email']).first()
        if user:
            return Response({'error': 'User already exists.'}, status=409)
        
        if not user_data.get('userRole'):
            user_data['userRole'] = UserRoleChoices.patient
        
        new_user = User(
            first_name=user_data['first_name'],
            last_name=user_data['last_name'],
            email=user_data['email'],
            user_role=user_data['userRole'],
            phone=user_data['phone'],
            gender=user_data['gender'],
            dob=user_data['dob'],
        )
        new_user.set_password(user_data['password'])
        new_user.is_active = False
        new_user.save()
        patient_profile = PatientProfile(user=new_user)
        patient_profile.save()

        current_site = get_current_site(request)
        subject = "Welcome to HealthConnect"
        html_message = render_to_string('new-email.html', {  
        'user': new_user,  
        'domain': current_site.domain,  
        'uid':urlsafe_base64_encode(force_bytes(new_user.pk)),  
        'token':generate_token.make_token(new_user),  
        })
        plain_message = strip_tags(html_message)
        email = EmailMultiAlternatives(
            from_email=settings.EMAIL_HOST_USER,
            to=[new_user.email],
            body=plain_message,
            subject=subject,
        )
        email.attach_alternative(html_message, 'text/html')
        email.send()

        return Response({'message': 'User registered successfully', 'status': 201}, status=201)

@api_view(['POST', 'OPTIONS'])
@permission_classes([IsAuthenticated])
def change_availability(request):
    user = request.user
    if user.user_role != UserRoleChoices.health_professional:
        return Response({'error': 'Only health professionals can change availability'}, status=403)

    doctor = DoctorProfile.objects.filter(user=user).first()
    if not doctor:
        return Response({'error': 'Doctor profile not found'}, status=404)

    doctor.available = not doctor.available
    doctor.save()

    return Response({'message': 'Availability changed successfully'}, status=200)

@api_view(['GET', 'OPTIONS'])
@permission_classes([IsAuthenticated])
def get_health_metrics(request):
    user = request.user

    metrics = PatientProfile.objects.filter(user=user).first()
    past_metrics = PastMetricsSerializer(PastMetrics.objects.filter(user=user).all(), many=True).data

    if not metrics:
        return Response({'error': 'Metrics not found'}, status=404)

    metrics = PatientProfileSerializer(metrics).data
    print(metrics)

    return Response({'metrics': metrics, 'past_metrics': past_metrics}, status=200)


@api_view(['POST', 'OPTIONS'])
@permission_classes([IsAuthenticated])
def update_health_metrics(request):
    user = request.user
    metrics, created = PatientProfile.objects.get_or_create(user=user)
    print(request.data)
    if not metrics:
        metrics = PatientProfile.objects.create(user=user)


    PastMetrics.objects.create(
        user=user,
        weight=metrics.weight,
        height=metrics.height,
        blood_pressure=metrics.blood_pressure,
        blood_glucose=metrics.blood_glucose,
        updated_field=list(request.data.keys())[0]
    )
    serializer = PatientProfileSerializer(metrics, data=request.data, partial=True)
    if serializer.is_valid():
        serializer.save()
        return Response({'message': 'Metrics updated successfully'}, status=200)
    return Response(serializer.errors, status=400)

@api_view(['GET', 'OPTIONS'])
@permission_classes([IsAuthenticated])
def get_profile(request):
    user = request.user
    print('user', user)
    if user.user_role == UserRoleChoices.health_professional:
        doctor = DoctorProfile.objects.filter(user=user).first()
        if not doctor:
            return Response({'error': 'Doctor profile not found'}, status=404)
        doctor = DoctorProfileSerializer(doctor).data
        return Response({'patient': doctor}, status=200)
    else:
        patient = PatientProfile.objects.filter(user=user).first()
        if not patient:
            return Response({'error': 'Patient profile not found'}, status=404)
        patient = PatientProfileSerializer(patient).data
        return Response({'patient': patient}, status=200)


logging.basicConfig(level=logging.DEBUG)

@csrf_exempt
def github_webhook(request):
    """
    Handle GitHub webhook events for repository updates.

    Automatically pulls updates from the main branch of the specified repository
    and triggers background tasks including dependency installation, testing,
    migrations, and WSGI application reload.

    Returns HTTP responses based on webhook event outcomes.
    """
    if request.method == 'POST':
        repo_path = '/home/healthconnectapp/HealthCare---Telemedicine-Platform'
        try:
            repo = git.Repo(repo_path)
        except git.exc.NoSuchPathError:
            logging.error(f'No such path: {repo_path}')
            return HttpResponse('Invalid repository path', status=400)
        except git.exc.InvalidGitRepositoryError:
            logging.error(f'Invalid Git repository: {repo_path}')
            return HttpResponse('Invalid Git repository', status=400)
        origin = repo.remotes.origin
        try:
            origin.fetch()
            # Checkout the 'main' branch and pull the latest changes
            if 'main' in repo.heads:
                repo.heads['main'].checkout()
            else:
                repo.create_head('main', origin.refs.main).set_tracking_branch(origin.refs.main).checkout()
            origin.pull()
        except git.exc.GitCommandError as e:
            logging.error(f'Git command error: {e}')
            return HttpResponse('Error during git operations', status=500)
    
        Thread(target=background_tasks).start()
        return HttpResponse('Webhook received', status=200)
    else:
        return HttpResponse(status=400)

def background_tasks():
    """
    Perform background tasks triggered by GitHub webhook events.

    Tasks include installing dependencies, running tests, executing migrations,
    and reloading the WSGI application.

    Logs errors encountered during these tasks.
    """
    # Install dependencies
    repo_path = '/home/healthconnectapp/HealthCare---Telemedicine-Platform/backend'

    try:
        requirements_file = os.path.join(repo_path, 'requirements.txt')
        subprocess.check_call(['pip', 'install', '-r', requirements_file])
    except subprocess.CalledProcessError as e:
        logging.error(f'Error installing dependencies: {e}')

    # Run tests
    try:
        manage_py = os.path.join(repo_path, 'manage.py')
        subprocess.check_call(['python', manage_py, 'test'])
    except subprocess.CalledProcessError as e:
        logging.error(f'Test execution failed: {e}')

    try:
        manage_py = os.path.join(repo_path, 'manage.py')
        subprocess.check_call(['python', manage_py, 'makemigrations'])
        subprocess.check_call(['python', manage_py, 'migrate'])
    except subprocess.CalledProcessError as e:
        logging.error(f'Migration execution failed: {e}')

    # Reload the WSGI application
    try:
        os.system('touch /var/www/healthconnectapp_pythonanywhere_com_wsgi.py')
    except OSError as e:
        logging.error(f'Error reloading WSGI application: {e}')
