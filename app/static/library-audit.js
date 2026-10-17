(function(){
  const form=document.querySelector('[data-duplicate-batch]');
  if(form){
    const choices=[...form.querySelectorAll('[data-delete-copy]')];
    const count=form.querySelector('[data-selection-count]');
    const error=form.querySelector('[data-selection-error]');
    const review=form.querySelector('.bulk-review');
    function update(){
      const selected=choices.filter(input=>input.checked);
      const bytes=selected.reduce((total,input)=>total+Number(input.dataset.size||0),0);
      const size=bytes>=1e12 ? `${(bytes/1e12).toFixed(1)} TB` : `${(bytes/1e9).toFixed(1)} GB`;
      count.textContent=`${selected.length} to delete${selected.length ? ` · ${size}` : ''}`;
      const groups=new Map();
      for(const input of choices){
        const row=input.closest('[data-duplicate-group]');
        if(!groups.has(row.dataset.duplicateGroup)) groups.set(row.dataset.duplicateGroup,[]);
        groups.get(row.dataset.duplicateGroup).push(input);
      }
      const invalid=[...groups.values()].find(group=>group.every(input=>input.checked));
      error.textContent=invalid ? `Keep at least one copy of ${invalid[0].closest('tr').dataset.title}.` : '';
      review.disabled=!selected.length||!!invalid;
      for(const input of choices) input.closest('tr').classList.toggle('marked-delete',input.checked);
    }
    form.addEventListener('change',update);
    form.querySelector('[data-keep-all]').addEventListener('click',()=>{
      for(const input of choices) input.closest('fieldset').querySelector('[value="keep"]').checked=true;
      update();
    });
    form.querySelector('[data-select-lower]').addEventListener('click',()=>{
      for(const input of choices){
        const choice=input.closest('fieldset').querySelector(input.dataset.lowerResolution==='true' ? '[value="delete"]' : '[value="keep"]');
        choice.checked=true;
      }
      update();
    });
    form.addEventListener('submit',event=>{update();if(review.disabled)event.preventDefault();});
    update();
  }
  const confirmation=document.querySelector('[data-batch-confirm]');
  if(confirmation) confirmation.addEventListener('submit',()=>{
    confirmation.querySelector('button').disabled=true;
    confirmation.querySelector('[data-batch-status]').textContent='Deleting selected copies…';
  });
})();
