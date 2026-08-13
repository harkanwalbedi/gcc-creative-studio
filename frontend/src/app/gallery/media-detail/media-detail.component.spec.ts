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

import {NO_ERRORS_SCHEMA} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {MatDialog} from '@angular/material/dialog';
import {MatSnackBar} from '@angular/material/snack-bar';
import {ActivatedRoute, Router, convertToParamMap} from '@angular/router';
import {of, throwError} from 'rxjs';

import {MediaDetailComponent} from './media-detail.component';
import {GalleryService} from '../gallery.service';
import {GalleryItem} from '../../common/models/gallery-item.model';
import {MediaItem} from '../../common/models/media-item.model';
import {VpeScreeningResponse} from '../../common/models/vpe.model';
import {VpeUpscaleDialogComponent} from '../../common/components/vpe-upscale-dialog/vpe-upscale-dialog.component';
import {AuthService} from '../../common/services/auth.service';
import {LoadingService} from '../../common/services/loading.service';
import {WorkspaceStateService} from '../../services/workspace/workspace-state.service';
import {VpeService} from '../../services/vpe/vpe.service';

describe('MediaDetailComponent', () => {
  let component: MediaDetailComponent;
  let fixture: ComponentFixture<MediaDetailComponent>;
  let galleryService: jasmine.SpyObj<GalleryService>;
  let vpeService: jasmine.SpyObj<VpeService>;
  let workspaceState: jasmine.SpyObj<WorkspaceStateService>;
  let dialog: jasmine.SpyObj<MatDialog>;
  let loadingService: jasmine.SpyObj<LoadingService>;
  let snackBar: jasmine.SpyObj<MatSnackBar>;
  let authService: jasmine.SpyObj<AuthService>;

  const ITEM_ID = 5;

  const screening: VpeScreeningResponse = {
    capability_id: 'video_upscale',
    screening: 'not_ruled_out',
    offer: true,
    findings: [],
  };

  const item = (overrides: Partial<GalleryItem> = {}): GalleryItem =>
    ({
      id: ITEM_ID,
      itemType: 'media_item',
      mimeType: 'video/mp4',
      gcsUris: [],
      presignedUrls: [],
      ...overrides,
    }) as unknown as GalleryItem;

  /**
   * The constructor fetches immediately, so every provider has to be in
   * place before the component exists - which is why this builds the
   * TestBed per test rather than in a shared beforeEach.
   */
  const build = () => {
    TestBed.configureTestingModule({
      declarations: [MediaDetailComponent],
      providers: [
        {
          provide: ActivatedRoute,
          useValue: {
            paramMap: of(convertToParamMap({id: String(ITEM_ID)})),
            snapshot: {queryParamMap: convertToParamMap({})},
          },
        },
        {provide: Router, useValue: {url: '/gallery/5', navigate: () => {}}},
        {provide: GalleryService, useValue: galleryService},
        {provide: LoadingService, useValue: loadingService},
        {provide: MatSnackBar, useValue: snackBar},
        {provide: AuthService, useValue: authService},
        {provide: WorkspaceStateService, useValue: workspaceState},
        {provide: VpeService, useValue: vpeService},
        {provide: MatDialog, useValue: dialog},
      ],
      schemas: [NO_ERRORS_SCHEMA],
    });

    fixture = TestBed.createComponent(MediaDetailComponent);
    component = fixture.componentInstance;
  };

  beforeEach(() => {
    galleryService = jasmine.createSpyObj('GalleryService', [
      'getMedia',
      'getAsset',
    ]);
    vpeService = jasmine.createSpyObj('VpeService', ['screenForUpscale']);
    workspaceState = jasmine.createSpyObj('WorkspaceStateService', [
      'getActiveWorkspaceId',
    ]);
    dialog = jasmine.createSpyObj('MatDialog', ['open']);
    loadingService = jasmine.createSpyObj('LoadingService', ['show', 'hide']);
    snackBar = jasmine.createSpyObj('MatSnackBar', ['open']);
    authService = jasmine.createSpyObj('AuthService', ['isUserAdmin']);

    authService.isUserAdmin.and.returnValue(false);
    galleryService.getMedia.and.returnValue(of(item()));
    vpeService.screenForUpscale.and.returnValue(of(screening));
    workspaceState.getActiveWorkspaceId.and.returnValue(1);
    dialog.open.and.returnValue({
      afterClosed: () => of(undefined),
    } as ReturnType<MatDialog['open']>);
  });

  it('should create', () => {
    build();

    expect(component).toBeTruthy();
  });

  describe('upscale screening', () => {
    it('screens a stored video once it has loaded', () => {
      build();

      expect(vpeService.screenForUpscale).toHaveBeenCalledWith(ITEM_ID);
      expect(component.vpeScreening).toEqual(screening);
    });

    it('does not screen an image', () => {
      galleryService.getMedia.and.returnValue(
        of(item({mimeType: 'image/png'})),
      );

      build();

      expect(vpeService.screenForUpscale).not.toHaveBeenCalled();
      expect(component.vpeScreening).toBeUndefined();
    });

    it('does not screen an item with no mime type at all', () => {
      galleryService.getMedia.and.returnValue(
        of(item({mimeType: undefined as unknown as string})),
      );

      build();

      expect(vpeService.screenForUpscale).not.toHaveBeenCalled();
    });

    /**
     * A source asset has no `id` the VPE routes recognise as a gallery row,
     * so screening one would 404 on every detail page view.
     */
    it('does not screen a source asset even when it is a video', () => {
      galleryService.getAsset.and.returnValue(
        of(item({itemType: 'source_asset'} as Partial<GalleryItem>)),
      );
      galleryService.getMedia.and.returnValue(
        of(item({itemType: 'source_asset'} as Partial<GalleryItem>)),
      );

      build();

      expect(vpeService.screenForUpscale).not.toHaveBeenCalled();
    });

    /**
     * This runs on every detail page view, so a screening call that cannot
     * complete leaves the button absent rather than interrupting someone
     * who is only looking at the media.
     */
    it('fails silently, leaving the action unoffered', () => {
      const consoleError = spyOn(console, 'error');
      vpeService.screenForUpscale.and.returnValue(
        throwError(() => new Error('not allowlisted')),
      );

      build();

      expect(component.vpeScreening).toBeUndefined();
      expect(snackBar.open).not.toHaveBeenCalled();
      expect(
        consoleError.calls
          .allArgs()
          .some(args => String(args[0]).includes('VPE screening failed')),
      ).toBeTrue();
    });
  });

  describe('handleUpscaleClick', () => {
    const click = () =>
      component.handleUpscaleClick({
        mediaItem: {id: ITEM_ID} as MediaItem,
        selectedIndex: 2,
      });

    it('opens the dialog with the workspace, row and screening', () => {
      build();

      click();

      expect(dialog.open).toHaveBeenCalled();
      const [openedComponent, config] = dialog.open.calls.mostRecent().args;
      expect(openedComponent).toBe(VpeUpscaleDialogComponent);
      expect(config?.data).toEqual({
        workspaceId: 1,
        mediaItemId: ITEM_ID,
        mediaIndex: 2,
        screening,
      });
    });

    it('does nothing when the item was never screened', () => {
      vpeService.screenForUpscale.and.returnValue(
        throwError(() => new Error('not allowlisted')),
      );
      spyOn(console, 'error');
      build();

      click();

      expect(dialog.open).not.toHaveBeenCalled();
    });

    it('refuses without an active workspace rather than opening the dialog', () => {
      workspaceState.getActiveWorkspaceId.and.returnValue(
        null as unknown as number,
      );
      const consoleError = spyOn(console, 'error');
      build();

      click();

      expect(dialog.open).not.toHaveBeenCalled();
      expect(
        consoleError.calls
          .allArgs()
          .some(args => String(args[0]).includes('Start upscale')),
      ).toBeTrue();
    });

    it('says nothing when the dialog is cancelled', () => {
      const consoleError = spyOn(console, 'error');
      build();

      click();

      expect(consoleError.calls.allArgs().length).toBe(0);
    });
  });
});
