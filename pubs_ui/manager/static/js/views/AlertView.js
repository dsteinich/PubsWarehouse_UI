/* jslint browser: true */

define([
	'handlebars',
	'bootstrap',
	'views/BaseView',
	'text!hb_templates/alert.hbs'
], function(Handlebars, bootstrap, BaseView, hbTemplate) {
	"use strict";

	var view = BaseView.extend({

		ALERT_KINDS : {
			success : 'alert-success',
			info : 'alert-info',
			warning : 'alert-warning',
			danger : 'alert-danger'
		},

		template: Handlebars.compile(hbTemplate),

		initialize : function(options) {
			BaseView.prototype.initialize.apply(this, arguments);
			this.context.alertKind = '';
			this.context.message = '';
		},

		showAlert : function(kind, message) {
			this.context.alertKind = kind;
			this.context.message = message;
			if (this.$('.alert').length === 0) {
				this.render();
			}
			else {
				this.$('.alert').removeClass(this.context.alertKind).addClass(kind);
				this.$('.alert-message').html(message);

				this.context.alertKind = kind;
				this.context.message = message;
			}
		},

		showSuccessAlert : function(message) {
			this.showAlert(this.ALERT_KINDS.success, message);
		},
		showInfoAlert : function(message) {
			this.showAlert(this.ALERT_KINDS.info, message);
		},
		showWarningAlert : function(message) {
			this.showAlert(this.ALERT_KINDS.warning, message);
		},
		showDangerAlert : function(message) {
			this.showAlert(this.ALERT_KINDS.danger, message);
		}
	});

	return view;
});