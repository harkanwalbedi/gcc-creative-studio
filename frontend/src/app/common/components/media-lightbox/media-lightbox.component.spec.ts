/**
 * Copyright 2025 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import {ComponentFixture, TestBed} from '@angular/core/testing';
import {DebugElement, NO_ERRORS_SCHEMA} from '@angular/core';
import {By} from '@angular/platform-browser';
import {NoopAnimationsModule} from '@angular/platform-browser/animations';
import {Clipboard} from '@angular/cdk/clipboard';
import {Location} from '@angular/common';
import {MatDialog} from '@angular/material/dialog';
import {MatMenuModule} from '@angular/material/menu';
import {MatSnackBar} from '@angular/material/snack-bar';
import {MatTooltip, MatTooltipModule} from '@angular/material/tooltip';
import {ActivatedRoute, Router} from '@angular/router';

import {MediaLightboxComponent} from './media-lightbox.component';
import {StudioButtonComponent} from '../studio-button/studio-button.component';
import {MediaItem} from '../../models/media-item.model';
import {TagsService} from '../../services/tags.service';
import {WorkspaceStateService} from '../../../services/workspace/workspace-state.service';

/** Icon ligature plus label, which is all a rendered action button reads as. */
const EDIT = 'edit';
const GENERATE_VIDEO = 'videocam';
const VTO = 'checkroom';
const TAGS = 'label';
const OMNI = 'layers Edit with Omni';
const EXTEND = 'auto_awesome Extend';
const CONCATENATE = 'add_to_photos Concatenate';

describe('MediaLightboxComponent', () => {
  let component: MediaLightboxComponent;
  let fixture: ComponentFixture<MediaLightboxComponent>;

  const videoItem = (
    gcsUris: string[] = ['gs://bucket/clip_0.mp4'],
  ): MediaItem => ({
    id: 7,
    mimeType: 'video/mp4',
    gcsUris,
    presignedUrls: ['https://storage.test/clip_0.mp4'],
    presignedThumbnailUrls: ['https://storage.test/clip_0.png'],
  });

  const imageItem = (): MediaItem => ({
    id: 9,
    mimeType: 'image/png',
    gcsUris: ['gs://bucket/image_0.png'],
    presignedUrls: ['https://storage.test/image_0.png'],
  });

  /**
   * Assigning the item rather than binding it keeps PhotoSwipe out of the
   * test: it is only wired up from ngOnChanges, which a direct assignment
   * does not run.
   */
  function show(item: MediaItem): void {
    component.mediaItem = item;
    fixture.detectChanges();
  }

  function actionButton(label: string): DebugElement | undefined {
    return fixture.debugElement
      .queryAll(By.css('studio-button'))
      .find(
        candidate =>
          (candidate.nativeElement.textContent || '')
            .replace(/\s+/g, ' ')
            .trim() === label,
      );
  }

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [MediaLightboxComponent, StudioButtonComponent],
      imports: [MatMenuModule, MatTooltipModule, NoopAnimationsModule],
      providers: [
        {provide: Clipboard, useValue: {copy: jasmine.createSpy('copy')}},
        {provide: MatSnackBar, useValue: {open: jasmine.createSpy('open')}},
        {provide: MatDialog, useValue: {open: jasmine.createSpy('open')}},
        {
          provide: Router,
          useValue: {
            url: '/gallery/7',
            createUrlTree: jasmine.createSpy('createUrlTree'),
            serializeUrl: jasmine.createSpy('serializeUrl').and.returnValue(''),
          },
        },
        {
          provide: Location,
          useValue: {replaceState: jasmine.createSpy('replaceState')},
        },
        {
          provide: ActivatedRoute,
          useValue: {snapshot: {queryParamMap: {get: () => null}}},
        },
        {provide: TagsService, useValue: {}},
        {provide: WorkspaceStateService, useValue: {}},
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();

    fixture = TestBed.createComponent(MediaLightboxComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('offers no video action to a host that wired none up', () => {
    show(videoItem());

    expect(actionButton(OMNI)).toBeUndefined();
    expect(actionButton(EXTEND)).toBeUndefined();
    expect(actionButton(CONCATENATE)).toBeUndefined();
  });

  it('offers the video actions the host opted into', () => {
    component.showOmniButton = true;
    component.showExtendButton = true;
    component.showConcatenateButton = true;
    show(videoItem());

    for (const label of [OMNI, EXTEND, CONCATENATE]) {
      const button = actionButton(label);
      expect(button).withContext(label).toBeDefined();
      expect(button!.query(By.css('button')).nativeElement.disabled)
        .withContext(label)
        .toBeFalse();
    }
  });

  it('offers no image action to a host that wired none up', () => {
    show(imageItem());

    expect(actionButton(EDIT)).toBeUndefined();
    expect(actionButton(GENERATE_VIDEO)).toBeUndefined();
    expect(actionButton(VTO)).toBeUndefined();
  });

  it('offers the image actions the host opted into', () => {
    component.showEditButton = true;
    component.showGenerateVideoButton = true;
    component.showVtoButton = true;
    show(imageItem());

    expect(actionButton(EDIT)).toBeDefined();
    expect(actionButton(GENERATE_VIDEO)).toBeDefined();
    expect(actionButton(VTO)).toBeDefined();
  });

  it('keeps the video actions of an image host away from a video', () => {
    component.showEditButton = true;
    component.showGenerateVideoButton = true;
    component.showVtoButton = true;
    show(videoItem());

    expect(actionButton(EDIT)).toBeUndefined();
    expect(actionButton(GENERATE_VIDEO)).toBeUndefined();
    expect(actionButton(VTO)).toBeUndefined();
  });

  it('shows Edit with Omni disabled, with the reason, for a clip with no stored file', () => {
    component.showOmniButton = true;
    show(videoItem([]));

    const button = actionButton(OMNI);
    expect(button).toBeDefined();
    expect(button!.query(By.css('button')).nativeElement.disabled).toBeTrue();
    expect(button!.injector.get(MatTooltip).message).toBe(
      component.noStoredClipReason,
    );
  });

  it('does not hand off a clip with no stored file', () => {
    const handedOff = jasmine.createSpy('handedOff');
    component.editWithOmniClicked.subscribe(handedOff);
    show(videoItem([]));

    component.onEditWithOmniClick();

    expect(handedOff).not.toHaveBeenCalled();
  });

  it('hides the tag button where the item id is not a media item id', () => {
    component.showTagsButton = false;
    show(imageItem());

    expect(actionButton(TAGS)).toBeUndefined();
  });
});
