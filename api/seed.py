#!/usr/bin/env python3
"""
Seed script for InfinityScan database.
Creates admin user and sample data.
"""

import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from pathlib import Path

# Configure before importing models
# Use SQLite for local development if no DATABASE_URL is set
DATABASE_URL = os.environ.get(
    "DATABASE_URL", 
    "sqlite:///./infinityscan.db"
)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    Base, User, Series, Chapter, Page, 
    ContentType, ReadingMode, SeriesStatus, UserRole
)
from auth import hash_password


def create_tables():
    """Create all tables."""
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)
    return engine


def create_admin_user(session):
    """Create admin user with credentials from environment variables."""
    admin_username = os.environ.get("ADMIN_USERNAME", "Conan")
    admin_password = os.environ.get("ADMIN_PASSWORD")
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@infinityscan.local")
    
    if not admin_password:
        print("WARNING: ADMIN_PASSWORD not set. Admin user will not be created.")
        print("Set ADMIN_PASSWORD environment variable to create an admin user.")
        return None
    
    admin = session.query(User).filter(User.username == admin_username).first()
    if admin:
        print(f"Admin user '{admin_username}' already exists")
        return admin
    
    admin = User(
        username=admin_username,
        email=admin_email,
        hashed_password=hash_password(admin_password),
        role=UserRole.admin,
        is_active=True,
    )
    session.add(admin)
    session.commit()
    print(f"Created admin user: {admin_username}")
    return admin


def create_sample_series(session):
    """Create sample series with chapters from the folder."""
    # Get manga folder path - must be configured via environment variable
    manga_base = os.environ.get("MANGA_LOCAL_PATH")
    if not manga_base:
        print("WARNING: MANGA_LOCAL_PATH not set. Sample series will not be seeded.")
        print("Set MANGA_LOCAL_PATH to your manga directory to seed sample data.")
        return None
    
    manga_base_path = Path(manga_base)
    
    if not manga_base_path.exists():
        print(f"WARNING: Manga folder not found: {manga_base_path}")
        return None
    
    # Get all subdirectories that might be manga series
    # Look for folders with chapter subdirectories (numbered 0, 1, 2...)
    series_count = 0
    
    for item in manga_base_path.iterdir():
        if not item.is_dir():
            continue
            
        # Check if this folder contains chapter subdirectories
        chapter_dirs = [d for d in item.iterdir() if d.is_dir() and d.name.isdigit()]
        
        if not chapter_dirs:
            continue
        
        # Check if series already exists
        slug = item.name
        existing = session.query(Series).filter(Series.slug == slug).first()
        
        if existing:
            print(f"Series '{slug}' already exists, skipping")
            continue
        
        # Create series
        series = Series(
            slug=slug,
            title=slug.replace("-", " ").replace("_", " ").title(),
            synopsis=f"Local manga series: {slug}",
            content_type=ContentType.manga,
            status=SeriesStatus.ongoing,
            default_reading_mode=ReadingMode.continuous,
            year=datetime.now().year,
            is_nsfw=False,
        )
        session.add(series)
        session.flush()
        
        # Sort and add chapters
        chapter_dirs.sort(key=lambda x: int(x.name))
        
        for chapter_dir in chapter_dirs:
            chapter_num = int(chapter_dir.name)
            
            # Get page files
            pages = sorted(chapter_dir.glob("*.jpg")) + sorted(chapter_dir.glob("*.png"))
            
            if not pages:
                continue
                
            chapter = Chapter(
                series_id=series.id,
                number=chapter_num,
                title=f"Chapter {chapter_num}",
                language="en",
                page_count=len(pages),
                published_at=datetime.now(),
            )
            session.add(chapter)
            session.flush()
            
            # Add pages
            for i, page_file in enumerate(pages, 1):
                page = Page(
                    chapter_id=chapter.id,
                    page_number=i,
                    object_key=f"{slug}/{chapter_num}/{page_file.name}",
                )
                session.add(page)
        
        session.commit()
        print(f"Added series: {slug} ({len(chapter_dirs)} chapters)")
        series_count += 1
    
    if series_count == 0:
        print("No manga series found in the folder")
    else:
        print(f"Added {series_count} series")
    
    return series_count


def main():
    print("Setting up InfinityScan database...")
    
    try:
        engine = create_tables()
        Session = sessionmaker(bind=engine)
        session = Session()
        
        # Create admin user
        admin = create_admin_user(session)
        
        # Create sample series
        create_sample_series(session)
        
        print("\n✅ Database setup complete!")
        if admin:
            print(f"   Admin login: username={admin.username}, password=(set via ADMIN_PASSWORD)")
        else:
            print("   No admin user created (set ADMIN_PASSWORD to create one)")
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
