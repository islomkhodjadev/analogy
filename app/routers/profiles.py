"""
Browser Profile management endpoints.
Profiles persist login state (cookies, localStorage) across jobs.
Inspired by Browser Use Cloud's Profile concept.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.dependencies import get_db, get_current_user
from app.models.user import User
from app.models.profile import BrowserProfile
from app.schemas.profile import (
    ProfileCreate,
    ProfileUpdate,
    ProfileResponse,
    ProfileListResponse,
)

router = APIRouter()


def _profile_to_response(p: BrowserProfile) -> ProfileResponse:
    return ProfileResponse(
        id=str(p.id),
        domain=p.domain,
        name=p.name,
        login_email=p.login_email,
        has_cookies=bool(p.cookies_json),
        has_local_storage=bool(p.local_storage_json),
        is_active=p.is_active,
        last_used_at=p.last_used_at,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


@router.get("/", response_model=ProfileListResponse)
def list_profiles(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all browser profiles for current user."""
    profiles = (
        db.query(BrowserProfile)
        .filter(BrowserProfile.user_id == current_user.id)
        .order_by(BrowserProfile.created_at.desc())
        .all()
    )
    return ProfileListResponse(
        profiles=[_profile_to_response(p) for p in profiles],
        total=len(profiles),
    )


@router.post("/", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
def create_profile(
    body: ProfileCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new browser profile for a domain.

    The profile starts empty — cookies and localStorage will be populated
    automatically when a job using this profile completes a successful login.
    """
    # Check for existing profile on same domain
    existing = db.query(BrowserProfile).filter(
        BrowserProfile.user_id == current_user.id,
        BrowserProfile.domain == body.domain,
        BrowserProfile.is_active == True,
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Active profile already exists for domain '{}'. "
                   "Delete or deactivate it first.".format(body.domain),
        )

    profile = BrowserProfile(
        user_id=current_user.id,
        domain=body.domain,
        name=body.name or body.domain,
        login_email=body.login_email,
        login_password=body.login_password,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)

    return _profile_to_response(profile)


@router.get("/{profile_id}", response_model=ProfileResponse)
def get_profile(
    profile_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = db.query(BrowserProfile).filter(
        BrowserProfile.id == profile_id,
        BrowserProfile.user_id == current_user.id,
    ).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _profile_to_response(profile)


@router.patch("/{profile_id}", response_model=ProfileResponse)
def update_profile(
    profile_id: UUID,
    body: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = db.query(BrowserProfile).filter(
        BrowserProfile.id == profile_id,
        BrowserProfile.user_id == current_user.id,
    ).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    if body.name is not None:
        profile.name = body.name
    if body.login_email is not None:
        profile.login_email = body.login_email
    if body.login_password is not None:
        profile.login_password = body.login_password
    if body.is_active is not None:
        profile.is_active = body.is_active

    db.commit()
    db.refresh(profile)
    return _profile_to_response(profile)


@router.delete("/{profile_id}")
def delete_profile(
    profile_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = db.query(BrowserProfile).filter(
        BrowserProfile.id == profile_id,
        BrowserProfile.user_id == current_user.id,
    ).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    db.delete(profile)
    db.commit()
    return {"detail": "Profile deleted"}


@router.post("/{profile_id}/clear-state")
def clear_profile_state(
    profile_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Clear saved cookies and localStorage from a profile (force re-login)."""
    profile = db.query(BrowserProfile).filter(
        BrowserProfile.id == profile_id,
        BrowserProfile.user_id == current_user.id,
    ).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    profile.cookies_json = None
    profile.local_storage_json = None
    profile.session_storage_json = None
    db.commit()
    return {"detail": "Profile state cleared — next job will re-login"}
